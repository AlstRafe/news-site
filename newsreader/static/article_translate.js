(() => {
  "use strict";
  const T = NR.t; // a text in the page's language (i18n.js)
  // the path the site is served under ("/news" behind the front page; "" on its own)
  const base = document.querySelector('meta[name="app-base"]')?.content || "";
  const controls = document.getElementById("translation-controls");
  if (!controls) return;

  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  // テスト公開 (web/public.py): a visitor has no 翻訳設定 page to be sent to.
  const owner = !document.querySelector('meta[name="site-visitor"]');
  const articleId = Number(controls.dataset.articleId || 0);
  const targetSelect = document.getElementById("translation-target");
  const status = document.getElementById("translation-status");
  const reader = document.getElementById("reading-content");
  const toolbar = document.getElementById("selection-toolbar");
  const selectionResult = document.getElementById("selection-result");
  const wholeResult = document.getElementById("whole-result");
  const wholeButton = document.getElementById("translate-whole");
  const selectionButton = document.getElementById("translate-selection");
  let selectedText = "";
  let activeWordHighlight = null;
  let requestCounter = 0;
  // The popup follows what it explains: `anchor` returns the current on-screen rectangle of the
  // looked-up word or selection (null when it is gone), so the card can sit next to it and move with it.
  let anchor = () => null;
  const narrow = window.matchMedia("(max-width: 650px)");
  const PIN_KEY = "news-html-reader-translation-pinned";
  // Translation targets come from the server (base.html), one list for every page.
  const targetMenu = (() => {
    try {
      return JSON.parse(document.querySelector('meta[name="translation-targets"]')?.content || "{}");
    } catch (_) {
      return {};
    }
  })();
  const targetNames = Object.fromEntries(Object.entries(targetMenu).map(([code, t]) => [code, t.label]));

  let saved = null;
  try {
    saved = localStorage.getItem("news-html-reader-translation-target");
  } catch (_) {
    /* local storage can be disabled */
  }
  // 翻訳先 follows the page's language until the reader picks one
  const wanted = saved && targetNames[saved] ? saved : NR.target;
  if (targetNames[wanted] && !targetSelect.querySelector(`option[value="${wanted}"]`)?.disabled)
    targetSelect.value = wanted;
  targetSelect.addEventListener("change", () => {
    try {
      localStorage.setItem("news-html-reader-translation-target", targetSelect.value);
    } catch (_) {
      /* optional */
    }
  });

  async function postJson(url, data) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(data),
    });
    const payload = await response.json().catch(() => ({}));
    return { ok: response.ok, payload };
  }

  function makeParagraphs(target, text) {
    target.replaceChildren();
    for (const part of String(text || "")
      .split(/\n{1,}/)
      .filter(Boolean)) {
      const p = document.createElement("p");
      p.textContent = part;
      target.appendChild(p);
    }
  }

  async function copyText(text, button) {
    const originalLabel = button.textContent;
    const value = String(text || "").trim();
    if (!value) {
      button.textContent = T("コピーする内容なし");
      window.setTimeout(() => {
        button.textContent = originalLabel;
      }, 1400);
      return;
    }
    try {
      if (navigator.clipboard?.writeText && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
      } else {
        const area = document.createElement("textarea");
        area.value = value;
        area.setAttribute("readonly", "");
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        const copied = document.execCommand("copy");
        area.remove();
        if (!copied) throw new Error("copy failed");
      }
      button.textContent = T("コピーしました");
    } catch (_) {
      button.textContent = T("コピーできません");
    }
    window.setTimeout(() => {
      button.textContent = originalLabel;
    }, 1400);
  }

  function translationText(element) {
    return [...element.querySelectorAll("p")]
      .map((p) => p.textContent || "")
      .join("\n\n")
      .trim();
  }

  // 訳語の指定 (D-63): a short selection (a name) can be given a fixed translation.
  function offerFixedTerm(original, translation, target) {
    const link = document.getElementById("fix-term");
    if (!link) return;
    const short = (text) => text && text.length <= 40 && !/[\n<>"&]/.test(text);
    link.hidden = !(short(original) && short(translation));
    if (!link.hidden) {
      link.href = `${base}/translation-settings?${new URLSearchParams({ gsrc: original, gdst: translation, glang: target })}#glossary`;
    }
  }

  // 単語帳に入れる: a word or short phrase with its translation, the sentence it was in and the article.
  let wordToKeep = null;
  function offerWordBook(original, translation, target) {
    const button = document.getElementById("save-word");
    if (!button) return;
    const keepable = original && original.length <= 40 && translation && translation.length <= 200;
    wordToKeep = keepable ? { word: original, meaning: translation, target, sentence: currentSentence || "" } : null;
    button.hidden = !wordToKeep;
    button.disabled = false;
    button.textContent = T("単語帳に入れる");
  }
  document.getElementById("save-word")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (!wordToKeep) return;
    button.disabled = true;
    try {
      const { ok, payload } = await postJson(base + "/api/words", { ...wordToKeep, id: articleId || undefined });
      button.textContent = ok ? T("単語帳に入れました") : payload.error || T("入れられませんでした");
    } catch (_) {
      button.textContent = T("入れられませんでした");
      button.disabled = false;
    }
  });

  function clearSelectionDetails() {
    offerFixedTerm("", "", "");
    offerWordBook("", "", "");
    document.getElementById("selection-error").hidden = true;
    document.getElementById("selection-error").textContent = "";
    document.getElementById("selection-translation").replaceChildren();
    document.getElementById("language-details").replaceChildren();
  }

  function clearWordHighlight() {
    if (!activeWordHighlight?.isConnected) {
      activeWordHighlight = null;
      return;
    }
    const parent = activeWordHighlight.parentNode;
    activeWordHighlight.replaceWith(document.createTextNode(activeWordHighlight.textContent || ""));
    if (parent) parent.normalize();
    activeWordHighlight = null;
  }

  function highlightWord(range) {
    clearWordHighlight();
    if (!range || range.collapsed) return;
    const marker = document.createElement("span");
    marker.className = "word-highlight";
    try {
      marker.appendChild(range.extractContents());
      range.insertNode(marker);
      activeWordHighlight = marker;
    } catch (_) {
      activeWordHighlight = null;
    }
  }

  function addDictionaryLink(container, url, label) {
    if (!url || !/^https:\/\//i.test(url)) return;
    const p = document.createElement("p");
    p.className = "dictionary-source";
    const a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = `${label || T("辞書で確認")}`;
    p.appendChild(a);
    container.appendChild(p);
  }

  function addDetails(payload) {
    const details = document.getElementById("language-details");
    if (payload.ipa) {
      const p = document.createElement("p");
      const label = document.createElement("strong");
      label.textContent = `${payload.ipa_label || T("英国英語 IPA")}: `;
      p.append(label, document.createTextNode(payload.ipa));
      details.appendChild(p);
    }
    if (payload.hanja?.length) {
      const box = document.createElement("div");
      box.className = "dictionary-entries";
      const heading = document.createElement("strong");
      heading.textContent = T("漢字語・辞書の語義");
      box.appendChild(heading);
      const list = document.createElement("ul");
      for (const entry of payload.hanja) {
        const li = document.createElement("li");
        const reading = document.createElement("strong");
        reading.textContent = `${entry.word} → ${entry.hanja}`;
        li.appendChild(reading);
        if (entry.meaning) li.appendChild(document.createTextNode(` · ${entry.meaning}`));
        if (entry.source_url && /^https:\/\//i.test(entry.source_url)) {
          li.appendChild(document.createTextNode(" "));
          const a = document.createElement("a");
          a.href = entry.source_url;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          a.textContent = T("出典");
          li.appendChild(a);
        }
        list.appendChild(li);
      }
      box.appendChild(list);
      details.appendChild(box);
    } else if (payload.dictionary_configured === false) {
      const p = document.createElement("p");
      p.textContent = T("韓国語の漢字語情報には国立国語院APIキーが必要です。");
      if (owner) {
        const a = document.createElement("a");
        a.href = base + "/translation-settings";
        a.textContent = T("翻訳設定を開く");
        p.append(" ", a);
      }
      details.appendChild(p);
    } else if (
      payload.dictionary_label &&
      payload.dictionary_configured !== false &&
      selectedText.match(/[\uac00-\ud7a3]/)
    ) {
      const p = document.createElement("p");
      p.textContent = T("辞書で一致する漢字語は見つかりませんでした。");
      details.appendChild(p);
    }
    if (payload.zhuyin) {
      const p = document.createElement("p");
      const label = document.createElement("strong");
      label.textContent = T("注音符號: ");
      p.append(label, document.createTextNode(payload.zhuyin));
      details.appendChild(p);
    } else if (
      payload.dictionary_label &&
      selectedText.match(/[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/) &&
      !selectedText.match(/[\uac00-\ud7a3]/)
    ) {
      const p = document.createElement("p");
      p.textContent = T("教育部辞典で完全一致する見出しが見つかりませんでした。");
      details.appendChild(p);
    }
    if (payload.meaning) {
      const p = document.createElement("p");
      const label = document.createElement("strong");
      label.textContent = T("意味（{language}）: ", { language: payload.meaning_language || T("日本語") });
      p.append(label, document.createTextNode(payload.meaning));
      details.appendChild(p);
    }
    if (payload.dictionary_url) addDictionaryLink(details, payload.dictionary_url, payload.dictionary_label);
  }

  function isSheet() {
    return narrow.matches;
  }

  function setPinned(pinned) {
    selectionResult.classList.toggle("pinned", pinned);
    document.body.classList.toggle("tr-docked", pinned && !selectionResult.hidden);
    const button = document.getElementById("pin-selection");
    if (!button) return;
    button.setAttribute("aria-pressed", String(pinned));
    button.title = pinned ? T("固定を解除（単語の近くに表示）") : T("右側に固定（本文を隠さない）");
    button.setAttribute("aria-label", pinned ? T("固定を解除") : T("右側に固定"));
  }
  try {
    setPinned(localStorage.getItem(PIN_KEY) === "1");
  } catch (_) {
    setPinned(false);
  }

  // Desktop: a card just below the word (above it when there is no room), never covering it.
  // Phone: a bottom sheet; the page scrolls so the word stays visible above the sheet.
  function placePopup({ reveal = false } = {}) {
    if (selectionResult.hidden) return;
    const docked = selectionResult.classList.contains("pinned") && !isSheet();
    document.body.classList.toggle("tr-docked", docked);
    document.body.classList.toggle("tr-sheet-open", isSheet());
    if (docked || isSheet()) {
      selectionResult.style.left = "";
      selectionResult.style.top = "";
      if (isSheet() && reveal) keepAnchorAboveSheet();
      return;
    }
    const rect = anchor();
    const margin = 12;
    const box = selectionResult.getBoundingClientRect();
    if (!rect) return;
    const below = rect.bottom + 10;
    const above = rect.top - 10 - box.height;
    let top = below + box.height <= window.innerHeight - margin || above < margin ? below : above;
    top = Math.max(margin, Math.min(top, window.innerHeight - box.height - margin));
    const left = Math.max(margin, Math.min(rect.left - 16, window.innerWidth - box.width - margin));
    selectionResult.style.left = `${left}px`;
    selectionResult.style.top = `${top}px`;
  }

  function keepAnchorAboveSheet() {
    const rect = anchor();
    if (!rect) return;
    const sheetTop = selectionResult.getBoundingClientRect().top;
    const header = document.querySelector(".topbar")?.getBoundingClientRect().bottom || 0;
    if (rect.bottom > sheetTop - 16) {
      window.scrollBy({ top: rect.bottom - (sheetTop - 48), behavior: "smooth" });
    } else if (rect.top < Math.max(0, header)) {
      window.scrollBy({ top: rect.top - Math.max(0, header) - 48, behavior: "smooth" });
    }
  }

  function openPopup() {
    const wasHidden = selectionResult.hidden;
    selectionResult.hidden = false;
    if (wasHidden) selectionResult.classList.remove("dragging");
    selectionResult.style.transform = "";
    placePopup({ reveal: true });
  }

  function closePopup() {
    lastSelectionText = "";
    selectionResult.hidden = true;
    selectionResult.style.transform = "";
    document.body.classList.remove("tr-docked", "tr-sheet-open");
    clearWordHighlight();
    anchor = () => null;
  }

  // The sentence around what was looked up, so a word can be read in context ("文全体を訳す").
  let currentSentence = "";
  let currentParagraph = null; // the article paragraph the looked-up text is in
  function sentenceAround(range) {
    currentParagraph = null;
    if (!range) return "";
    const block = (
      range.startContainer.nodeType === Node.TEXT_NODE ? range.startContainer.parentElement : range.startContainer
    )?.closest?.("p");
    if (!block || !reader.contains(block)) return "";
    if (!block.classList.contains("bi-tr")) currentParagraph = block;
    const before = document.createRange();
    before.selectNodeContents(block);
    before.setEnd(range.startContainer, range.startOffset);
    const offset = before.toString().length;
    const text = block.textContent || "";
    if (typeof Intl.Segmenter === "function") {
      const locale = /[\uac00-\ud7a3]/.test(text) ? "ko" : /[\u3400-\u9fff]/.test(text) ? "zh-TW" : "en-GB";
      for (const part of new Intl.Segmenter(locale, { granularity: "sentence" }).segment(text)) {
        if (part.index <= offset && offset < part.index + part.segment.length) return part.segment.trim();
      }
    }
    const start = Math.max(text.lastIndexOf(". ", offset), text.lastIndexOf("。", offset), -1) + 1;
    const ends = [". ", "。", "? ", "! "].map((mark) => text.indexOf(mark, offset)).filter((i) => i >= 0);
    return text.slice(start, ends.length ? Math.min(...ends) + 1 : text.length).trim();
  }

  function resetSentence(sentence, looked) {
    currentSentence = sentence && sentence.length <= 1200 && sentence !== looked ? sentence : "";
    const button = document.getElementById("translate-sentence");
    const box = document.getElementById("sentence-tr");
    if (button) {
      button.hidden = !currentSentence;
      button.disabled = false;
    }
    if (box) box.hidden = true;
    // Without a sentence step (the whole sentence was selected), offer the paragraph at once.
    showParagraphButton(!currentSentence);
  }

  // "段落を対訳表示": the paragraph's translation goes under it in the article (like the 対訳
  // view, for one paragraph), so a long article need not be translated as a whole.
  function inlineTranslation(paragraph) {
    const next = paragraph?.nextElementSibling;
    return next && next.classList.contains("bi-tr") ? next : null;
  }

  function showParagraphButton(show) {
    const button = document.getElementById("translate-paragraph");
    if (!button) return;
    button.hidden = !(
      show &&
      currentParagraph?.isConnected &&
      !inlineTranslation(currentParagraph) &&
      (currentParagraph.textContent || "").trim().length <= 4000
    );
    button.disabled = false;
  }

  async function translateParagraphInline() {
    const paragraph = currentParagraph;
    const button = document.getElementById("translate-paragraph");
    if (!paragraph?.isConnected || inlineTranslation(paragraph)) return;
    const target = targetSelect.value;
    button.disabled = true;
    button.textContent = T("訳しています…");
    let text = "";
    let failure = "";
    try {
      const { ok, payload } = await postJson(base + "/api/translate", {
        text: (paragraph.textContent || "").trim(),
        target,
        plain: true,
      });
      if (ok && payload.translation) text = payload.translation;
      else failure = payload.translation_error || payload.error || T("翻訳できませんでした。");
    } catch (_) {
      failure = T("アプリに接続できませんでした。");
    }
    button.textContent = T("段落を対訳表示");
    button.disabled = false;
    if (failure) {
      const error = document.getElementById("selection-error");
      error.textContent = failure;
      error.hidden = false;
      placePopup();
      return;
    }
    if (inlineTranslation(paragraph)) return;
    const translated = document.createElement("p");
    translated.className = "bi-tr bi-one";
    translated.lang = langTags[target] || "";
    translated.textContent = text;
    paragraph.after(translated);
    closePopup();
    translated.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  document.getElementById("translate-paragraph")?.addEventListener("click", translateParagraphInline);

  async function translateSentence() {
    const button = document.getElementById("translate-sentence");
    const box = document.getElementById("sentence-tr");
    if (!currentSentence || !box) return;
    const sequence = requestCounter;
    button.disabled = true;
    document.getElementById("sentence-original").textContent = currentSentence;
    document.getElementById("sentence-translation").textContent = T("翻訳しています…");
    box.hidden = false;
    placePopup();
    let text;
    try {
      const { ok, payload } = await postJson(base + "/api/translate", {
        text: currentSentence,
        target: targetSelect.value,
        plain: true,
      });
      text =
        ok && payload.translation
          ? payload.translation
          : payload.translation_error || payload.error || T("翻訳できませんでした。");
    } catch (_) {
      text = T("アプリに接続できませんでした。");
    }
    if (sequence !== requestCounter) return;
    document.getElementById("sentence-translation").textContent = text;
    button.hidden = true;
    showParagraphButton(true);
    placePopup();
  }
  document.getElementById("translate-sentence")?.addEventListener("click", translateSentence);

  async function translateSelected(text, anchorFn) {
    const normalized = text.trim();
    if (!normalized) return;
    selectedText = normalized;
    anchor = anchorFn || (() => null);
    const sequence = ++requestCounter;
    document.getElementById("selected-original").textContent = normalized;
    const targetLabel = document.getElementById("selection-target");
    if (targetLabel) targetLabel.textContent = `→ ${targetNames[targetSelect.value] || ""}`;
    clearSelectionDetails();
    const output = document.getElementById("selection-translation");
    output.classList.add("loading");
    makeParagraphs(output, T("翻訳しています…"));
    openPopup();
    let payload;
    let ok = true;
    try {
      ({ ok, payload } = await postJson(base + "/api/translate", { text: normalized, target: targetSelect.value }));
    } catch (_) {
      payload = { translation_error: T("アプリに接続できませんでした。サーバーが起動しているか確認してください。") };
    }
    if (sequence !== requestCounter) return;
    output.classList.remove("loading");
    if (!ok && !payload.translation_error && !payload.error)
      payload.translation_error = T("翻訳要求に失敗しました。もう一度お試しください。");
    makeParagraphs(output, payload.translation || "");
    if (payload.translation_error || payload.error) {
      const error = document.getElementById("selection-error");
      error.textContent = payload.translation_error || payload.error;
      error.hidden = false;
      if (owner) {
        const link = document.createElement("a");
        link.href = base + "/translation-settings";
        link.textContent = T("翻訳設定を確認");
        document.getElementById("language-details").appendChild(link);
      }
    }
    addDetails(payload);
    if (payload.translation && !payload.translation_error) {
      offerFixedTerm(normalized, String(payload.translation).trim(), targetSelect.value);
      offerWordBook(normalized, String(payload.translation).trim(), targetSelect.value);
    }
    const lost =
      payload.translation && window.NumberCheck
        ? window.NumberCheck.missing(normalized, String(payload.translation))
        : [];
    if (lost.length) {
      // 訳文の数字チェック (D-69)
      const note = document.createElement("p");
      note.className = "num-check-card";
      note.textContent = T("訳文に見当たらない数字: {numbers}", { numbers: lost.join(T("・")) });
      document.getElementById("language-details").prepend(note);
    }
    placePopup({ reveal: true });
    if (payload.dictionary && payload.translation) loadSenses(normalized, targetSelect.value, sequence);
  }

  // Meanings by part of speech for an English word (Azure dictionary), shown under the
  // translation once they arrive; nothing is shown when the word is not in the dictionary.
  async function loadSenses(text, target, sequence) {
    const details = document.getElementById("language-details");
    const box = document.createElement("div");
    box.className = "senses loading";
    box.textContent = T("辞書を引いています…");
    details.prepend(box);
    let result;
    try {
      result = await postJson(base + "/api/dictionary", { text, target });
    } catch (_) {
      result = null;
    }
    if (sequence !== requestCounter || !box.isConnected) return;
    const senses = result?.ok && Array.isArray(result.payload.senses) ? result.payload.senses : [];
    if (!senses.length) {
      box.remove();
      placePopup({ reveal: true });
      return;
    }
    box.className = "senses";
    box.replaceChildren();
    for (const sense of senses) {
      if (!sense || !Array.isArray(sense.terms) || !sense.terms.length) continue;
      const row = document.createElement("p");
      const pos = document.createElement("span");
      pos.className = "pos";
      pos.textContent = String(sense.pos || "");
      row.append(pos, document.createTextNode(sense.terms.map(String).join("、")));
      box.appendChild(row);
    }
    const source = document.createElement("p");
    source.className = "senses-source";
    source.textContent = String(result.payload.label || T("辞書"));
    box.appendChild(source);
    placePopup({ reveal: true }); // the card grew: keep the word in view above a phone's sheet
  }

  function wordAtPoint(x, y) {
    let range = null;
    if (document.caretPositionFromPoint) {
      const pos = document.caretPositionFromPoint(x, y);
      if (pos) {
        range = document.createRange();
        range.setStart(pos.offsetNode, pos.offset);
        range.collapse(true);
      }
    } else if (document.caretRangeFromPoint) {
      range = document.caretRangeFromPoint(x, y);
    }
    if (!range || !reader.contains(range.startContainer) || range.startContainer.nodeType !== Node.TEXT_NODE) return "";
    const nodeText = range.startContainer.textContent || "";
    const offset = Math.min(range.startOffset, Math.max(0, nodeText.length - 1));
    const korean = /[\uac00-\ud7a3]/.test(nodeText);
    const chinese = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/.test(nodeText);
    const locale = korean ? "ko" : chinese ? "zh-TW" : "en-GB";
    if (typeof Intl.Segmenter === "function") {
      const segments = [...new Intl.Segmenter(locale, { granularity: "word" }).segment(nodeText)];
      let match = segments.find(
        (item) => item.isWordLike && item.index <= offset && offset < item.index + item.segment.length,
      );
      if (!match && offset > 0)
        match = segments.find(
          (item) => item.isWordLike && item.index < offset && offset <= item.index + item.segment.length,
        );
      if (!match) return null;
      const wordRange = document.createRange();
      wordRange.setStart(range.startContainer, match.index);
      wordRange.setEnd(range.startContainer, match.index + match.segment.length);
      return { word: match.segment, range: wordRange };
    }
    const before = nodeText.slice(0, offset + 1).match(/[\p{L}\p{N}]+$/u);
    const after = before ? null : nodeText.slice(offset).match(/^[\p{L}\p{N}]+/u);
    const word = before?.[0] || after?.[0] || "";
    if (!word) return null;
    const start = before ? offset + 1 - word.length : offset;
    const wordRange = document.createRange();
    wordRange.setStart(range.startContainer, start);
    wordRange.setEnd(range.startContainer, start + word.length);
    return { word, range: wordRange };
  }

  // Selecting text translates it directly — no extra button to hunt for. With a mouse that is
  // the moment the button is released; on a touch screen, once the selection has stopped
  // changing for a moment (the handles are being dragged until then).
  let lastSelectionText = "";
  function currentReaderSelection() {
    const selection = window.getSelection();
    const text = selection?.toString().trim() || "";
    if (!text || !selection.rangeCount) return null;
    const range = selection.getRangeAt(0);
    const node = range.commonAncestorContainer;
    const element = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
    if (!reader.contains(node) || element?.closest?.(".bi-tr")) return null;
    return { text, range: range.cloneRange() };
  }

  function translateCurrentSelection() {
    const picked = currentReaderSelection();
    if (!picked) return;
    toolbar.hidden = true;
    if (picked.text === lastSelectionText && !selectionResult.hidden) return; // already shown
    lastSelectionText = picked.text;
    clearWordHighlight();
    resetSentence(sentenceAround(picked.range), picked.text);
    translateSelected(picked.text, () => {
      const rect = picked.range.getBoundingClientRect();
      return rect && (rect.width || rect.height) ? rect : null;
    });
  }

  reader.addEventListener("mouseup", (event) => {
    if (event.button !== 0) return;
    window.setTimeout(translateCurrentSelection, 0);
  });

  let lastPointer = "mouse";
  let touching = false;
  document.addEventListener(
    "pointerdown",
    (event) => {
      lastPointer = event.pointerType || "mouse";
    },
    { capture: true, passive: true },
  );
  document.addEventListener(
    "touchstart",
    () => {
      touching = true;
    },
    { capture: true, passive: true },
  );
  let selectionTimer = 0;
  const settleSelection = (delay) => {
    window.clearTimeout(selectionTimer);
    selectionTimer = window.setTimeout(() => {
      if (!touching) translateCurrentSelection();
    }, delay);
  };
  document.addEventListener(
    "touchend",
    () => {
      touching = false;
      if (currentReaderSelection()) settleSelection(350);
    },
    { capture: true, passive: true },
  );
  document.addEventListener(
    "touchcancel",
    () => {
      touching = false;
    },
    { capture: true, passive: true },
  );
  document.addEventListener("selectionchange", () => {
    if (lastPointer !== "touch") return;
    settleSelection(700);
  });
  // Tapping / clicking outside the card (and outside the article text, where a tap looks up the
  // tapped word instead) closes it. A card docked to the side stays until closed.
  document.addEventListener("click", (event) => {
    if (selectionResult.hidden) return;
    if (selectionResult.contains(event.target) || toolbar.contains(event.target) || reader.contains(event.target))
      return;
    if (selectionResult.classList.contains("pinned") && !isSheet()) return;
    if (event.target.closest?.("#translation-controls")) return;
    closePopup();
  });

  reader.addEventListener("click", (event) => {
    if (event.target.closest?.(".bi-tr")) return; // translated text is not looked up again
    if (window.getSelection()?.toString().trim()) return;
    clearWordHighlight();
    const selected = wordAtPoint(event.clientX, event.clientY);
    if (selected) {
      toolbar.hidden = true;
      lastSelectionText = "";
      resetSentence(sentenceAround(selected.range), selected.word);
      highlightWord(selected.range);
      translateSelected(selected.word, () =>
        activeWordHighlight?.isConnected ? activeWordHighlight.getBoundingClientRect() : null,
      );
    }
  });

  selectionButton.addEventListener("mousedown", (event) => event.preventDefault());
  selectionButton.addEventListener("click", () => {
    const current = window.getSelection();
    const selection = current?.toString().trim() || selectedText;
    const range = current?.rangeCount ? current.getRangeAt(0).cloneRange() : null;
    toolbar.hidden = true;
    clearWordHighlight();
    translateSelected(selection, () => {
      const rect = range?.getBoundingClientRect();
      return rect && (rect.width || rect.height) ? rect : null;
    });
  });

  // ---- Side-by-side (対訳) view: each translated paragraph is inserted under its original ----
  // The text reads one of three ways: 原文, 対訳, or 訳文だけ (the 対訳 with the originals that
  // have a translation hidden, for a reader who does not read the source language). Which of
  // 対訳 / 訳文だけ a translation opens in is remembered per device.
  const biButton = document.getElementById("translate-bilingual");
  const onlyButton = document.getElementById("translate-only");
  const originalButton = document.getElementById("show-original");
  const ONLY_KEY = "news-html-reader-translation-only";
  const prefersOnly = () => {
    try {
      return localStorage.getItem(ONLY_KEY) === "1";
    } catch (_) {
      return false;
    }
  };
  function syncMode() {
    const only = Boolean(biTarget) && reader.classList.contains("tr-only");
    originalButton?.setAttribute("aria-pressed", String(!biTarget));
    biButton.setAttribute("aria-pressed", String(Boolean(biTarget) && !only));
    onlyButton?.setAttribute("aria-pressed", String(only));
    [originalButton, onlyButton].forEach((button) => {
      if (button) button.disabled = biButton.disabled;
    });
  }
  function setOnly(on, remember = true) {
    reader.classList.toggle("tr-only", on);
    if (remember) {
      try {
        localStorage.setItem(ONLY_KEY, on ? "1" : "0");
      } catch (_) {
        /* optional */
      }
    }
    syncMode();
  }
  const biTitle = document.getElementById("bi-title");
  const langTags = Object.fromEntries(Object.entries(targetMenu).map(([code, t]) => [code, t.tag]));
  let biTarget = "";

  // Sentence pairs (D-57): with the sentence lengths the server sends, each original sentence and
  // its translation become spans sharing data-pair; pointing at (or tapping) either lights both.
  const sum = (list) => list.reduce((total, n) => total + n, 0);
  function fitsSentences(node, lengths) {
    return (
      Array.isArray(lengths) &&
      lengths.length > 1 &&
      lengths.every(Number.isInteger) &&
      sum(lengths) === node.textContent.length
    );
  }
  function wrapSentences(node, lengths, prefix) {
    const text = node.textContent;
    const pieces = document.createDocumentFragment();
    let at = 0;
    lengths.forEach((n, k) => {
      const span = document.createElement("span");
      span.className = "sent";
      span.dataset.pair = `${prefix}-${k}`;
      span.textContent = text.slice(at, at + n);
      at += n;
      pieces.append(span);
    });
    node.replaceChildren(pieces);
  }
  function unwrapSentences() {
    reader.querySelectorAll(":scope > p:not(.bi-tr)").forEach((node) => {
      if (node.querySelector(".sent")) node.textContent = node.textContent;
    });
    reader.classList.remove("paired");
  }
  let litPair = "";
  function lightPair(pair) {
    if ((pair || "") === litPair) return;
    reader.querySelectorAll(".sent-on").forEach((node) => node.classList.remove("sent-on"));
    litPair = pair || "";
    if (litPair)
      reader.querySelectorAll(`.sent[data-pair="${litPair}"]`).forEach((node) => node.classList.add("sent-on"));
  }
  reader.addEventListener("pointerover", (event) => {
    if (event.pointerType === "mouse" && reader.classList.contains("paired"))
      lightPair(event.target.closest?.(".sent")?.dataset.pair);
  });
  reader.addEventListener("pointerleave", (event) => {
    if (event.pointerType === "mouse") lightPair("");
  });
  reader.addEventListener("pointerdown", (event) => {
    if (event.pointerType !== "mouse" && reader.classList.contains("paired"))
      lightPair(event.target.closest?.(".sent")?.dataset.pair);
  });

  function clearBilingual() {
    lightPair("");
    unwrapSentences();
    reader.querySelectorAll(".bi-tr").forEach((node) => node.remove());
    reader.classList.remove("bilingual");
    biTitle.hidden = true;
    biTitle.textContent = "";
    biTarget = "";
    syncMode();
    syncFloat();
    showTitleGloss();
  }

  // Unless the list's translated headlines are turned off (per device), the headline here is
  // translated too, from the same per-headline cache as the list (the page may arrive with it).
  async function showTitleGloss() {
    let on;
    try {
      on = localStorage.getItem("news-html-reader-list-gloss") !== "0";
    } catch (_) {
      on = true;
    }
    if (!on || biTarget || !articleId) return;
    const target = targetSelect.value;
    try {
      const { ok, payload } = await postJson(base + "/api/translate-titles", { ids: [articleId], target, auto: true });
      const text = ok && payload.translations ? payload.translations[String(articleId)] : "";
      if (!text || biTarget || target !== targetSelect.value) return;
      biTitle.textContent = text;
      biTitle.lang = langTags[target] || "";
      biTitle.classList.remove("kept");
      biTitle.hidden = false;
    } catch (_) {
      /* the headline stays untranslated */
    }
  }

  // Picture captions in the reader's language, under each original caption (same switch as the
  // translated headlines; each caption is kept on the server, so a reload costs nothing).
  async function showCaptions() {
    document.querySelectorAll(".cap-tr").forEach((node) => node.remove());
    let on;
    try {
      on = localStorage.getItem("news-html-reader-list-gloss") !== "0";
    } catch (_) {
      on = true;
    }
    const figures = [...document.querySelectorAll(".article-images figure")];
    if (!on || !articleId || !figures.some((f) => f.querySelector("figcaption"))) return;
    const target = targetSelect.value;
    try {
      const { ok, payload } = await postJson(base + "/api/translate-captions", { id: articleId, target });
      if (!ok || target !== targetSelect.value) return;
      (payload.captions || []).forEach((text, i) => {
        const figure = figures[i];
        const original = figure?.querySelector("figcaption");
        if (!text || !original || text === original.textContent.trim()) return;
        const line = document.createElement("p");
        line.className = "cap-tr";
        line.lang = langTags[target] || "";
        line.textContent = text;
        original.after(line);
      });
    } catch (_) {
      /* captions stay as they are */
    }
  }

  targetSelect.addEventListener("change", () => {
    reader.querySelectorAll(".bi-one").forEach((node) => node.remove()); // single paragraphs, old language
    showCaptions();
    if (!biTarget) {
      showTitleGloss();
      return;
    }
    clearBilingual();
    // With "open in 対訳" on, follow the new language instead of just closing.
    if (document.getElementById("bi-auto")?.checked) toggleBilingual(true);
    else status.textContent = T("翻訳先を変えたため対訳を閉じました。");
  });

  biButton.addEventListener("click", () => {
    if (biTarget && reader.classList.contains("tr-only")) setOnly(false);
    else {
      if (!biTarget) setOnly(false);
      toggleBilingual(false);
    }
  });
  onlyButton?.addEventListener("click", () => {
    if (biTarget && reader.classList.contains("tr-only")) {
      setOnly(false);
      return;
    }
    setOnly(true);
    if (!biTarget) toggleBilingual(false);
  });
  originalButton?.addEventListener("click", () => {
    if (biTarget) toggleBilingual(false);
  });
  // auto: opened by the page itself ("記事を開いたら対訳で表示する"), which the server holds back
  // once the month's automatic translations reach their limit; the button is never held back.
  async function toggleBilingual(auto = false) {
    if (biTarget) {
      clearBilingual();
      status.textContent = "";
      syncFloat();
      return;
    }
    const target = targetSelect.value;
    biButton.disabled = true;
    reader.classList.toggle("tr-only", prefersOnly());
    syncMode();
    syncFloat();
    status.textContent = T("対訳を作成しています…");
    try {
      const { ok, payload } = await postJson(base + "/api/translate-paragraphs", {
        id: articleId,
        target,
        auto: auto === true,
      });
      if (!ok || payload.error) throw new Error(payload.error || T("翻訳に失敗しました。"));
      const originals = [...reader.querySelectorAll(":scope > p:not(.bi-tr)")];
      if (!Array.isArray(payload.paragraphs) || payload.paragraphs.length !== originals.length) {
        throw new Error(T("段落の対応が取れませんでした。本文の下の「全文をまとめて翻訳する」をお試しください。"));
      }
      reader.querySelectorAll(".bi-tr").forEach((node) => node.remove()); // single-paragraph ones
      unwrapSentences();
      let paired = 0;
      let doubtful = 0;
      originals.forEach((paragraph, index) => {
        const text = payload.paragraphs[index];
        if (!text) return;
        const translated = document.createElement("p");
        translated.className = "bi-tr";
        translated.lang = langTags[target] || "";
        translated.textContent = text;
        paragraph.after(translated);
        // 訳文の数字チェック (D-69): figures of the original that the translation does not carry
        const lost = window.NumberCheck ? window.NumberCheck.missing(paragraph.textContent, text) : [];
        if (lost.length) {
          const note = document.createElement("p");
          note.className = "bi-tr num-check";
          note.setAttribute("role", "note");
          note.textContent = T("訳文に見当たらない数字: {numbers}（原文の数字を確かめてください）", {
            numbers: lost.join(T("・")),
          });
          translated.after(note);
          doubtful += 1;
        }
        const spans = payload.sentences?.[index];
        if (
          spans &&
          fitsSentences(paragraph, spans.src) &&
          fitsSentences(translated, spans.tr) &&
          spans.src.length === spans.tr.length
        ) {
          wrapSentences(paragraph, spans.src, `p${index}`);
          wrapSentences(translated, spans.tr, `p${index}`);
          paired += 1;
        }
      });
      reader.classList.toggle("paired", paired > 0);
      if (payload.title) {
        biTitle.classList.remove("kept");
        biTitle.textContent = payload.title;
        biTitle.lang = langTags[target] || "";
        biTitle.hidden = false;
      }
      reader.classList.add("bilingual");
      biTarget = target;
      // opened by itself, the 対訳 speaks for itself: only a warning takes room above the text
      status.textContent =
        (auto
          ? ""
          : T("{language}の対訳を表示しました。", { language: targetNames[target] }) +
            (reader.classList.contains("paired")
              ? T("文にカーソルを合わせる（タップする）と、対応する訳文が光ります。")
              : "")) + (doubtful ? T("{count} 段落で、原文の数字が訳文に見当たりません。", { count: doubtful }) : "");
    } catch (error) {
      status.textContent = error.message || T("翻訳に失敗しました。");
    } finally {
      biButton.disabled = false;
      syncMode();
      syncFloat();
    }
  }

  // Once the translation bar has scrolled away, a small floating button turns the 対訳 view on
  // or off from anywhere in the article, and keeps the paragraph being read where it was
  // (translations are inserted above it, which would otherwise push it down the screen).
  const biFloat = document.getElementById("bi-float");
  function syncFloat() {
    if (!biFloat) return;
    biFloat.disabled = biButton.disabled;
    biFloat.textContent = biButton.disabled ? T("訳しています…") : biTarget ? T("訳を隠す") : T("対訳");
    biFloat.setAttribute("aria-pressed", biTarget ? "true" : "false");
    biFloat.title = biButton.disabled
      ? ""
      : (biTarget ? T("対訳を閉じる") : T("この記事を対訳で表示")) +
        (status.textContent ? `（${status.textContent}）` : "");
  }
  if (biFloat && "IntersectionObserver" in window) {
    let barVisible = true;
    let textVisible = false;
    const update = () => {
      biFloat.hidden = barVisible || !textVisible;
    };
    new IntersectionObserver((entries) => {
      barVisible = entries[0].isIntersecting;
      update();
    }).observe(controls);
    new IntersectionObserver((entries) => {
      textVisible = entries[0].isIntersecting;
      update();
    }).observe(reader);
    biFloat.addEventListener("click", async () => {
      const onScreen = [...reader.querySelectorAll(":scope > p")].find(
        (p) => p.getClientRects().length && p.getBoundingClientRect().bottom > 80,
      );
      const before = onScreen?.getBoundingClientRect().top ?? 0;
      // a translation goes away on closing (訳文だけ): keep its original where it was instead
      const anchorParagraph = onScreen?.classList.contains("bi-tr") ? onScreen.previousElementSibling : onScreen;
      await toggleBilingual();
      // opening in 訳文だけ hides the original: its translation takes its place
      const next = anchorParagraph?.nextElementSibling;
      const shown =
        anchorParagraph?.isConnected && anchorParagraph.getClientRects().length
          ? anchorParagraph
          : next?.classList.contains("bi-tr")
            ? next
            : null;
      if (shown) window.scrollBy(0, shown.getBoundingClientRect().top - before);
    });
    syncFloat();
  }

  showTitleGloss();
  showCaptions();

  // "?tr=en" (from 読み比べ's 言語をそろえる): open the 対訳 view in that language for this visit
  // only (the saved 翻訳先 is not changed).
  const requested = new URLSearchParams(location.search).get("tr");
  const openedFor = requested && targetMenu[requested]?.available ? requested : "";
  if (openedFor) {
    targetSelect.value = openedFor;
    biButton.click();
  }

  // Reading progress: a thin bar under the header that fills as the text is read.
  const progress = document.getElementById("read-progress");
  if (progress) {
    let queued = false;
    const measure = () => {
      queued = false;
      const box = reader.getBoundingClientRect();
      const total = box.height - window.innerHeight * 0.6;
      const done = total > 0 ? Math.min(1, Math.max(0, -box.top / total)) : 1;
      progress.style.transform = `scaleX(${done.toFixed(3)})`;
    };
    window.addEventListener(
      "scroll",
      () => {
        if (!queued) {
          queued = true;
          requestAnimationFrame(measure);
        }
      },
      { passive: true },
    );
    measure();
  }

  // Remembered per device: open every article directly in the side-by-side view.
  const BI_AUTO_KEY = "news-html-reader-bilingual-auto";
  const biAuto = document.getElementById("bi-auto");
  if (biAuto) {
    // On unless turned off on this device: a Japanese reader opens an article to read it in Japanese.
    try {
      biAuto.checked = localStorage.getItem(BI_AUTO_KEY) !== "0";
    } catch (_) {
      biAuto.checked = true;
    }
    biAuto.addEventListener("change", () => {
      try {
        localStorage.setItem(BI_AUTO_KEY, biAuto.checked ? "1" : "0");
      } catch (_) {
        /* optional */
      }
      if (biAuto.checked && !biTarget) biButton.click();
    });
    if (biAuto.checked && !openedFor) toggleBilingual(true);
  }

  wholeButton.addEventListener("click", async () => {
    const target = targetSelect.value;
    wholeButton.disabled = true;
    status.textContent = T("全文を翻訳しています…");
    wholeResult.hidden = false;
    document.getElementById("whole-title").textContent = T("全文翻訳 · {language}", { language: targetNames[target] });
    makeParagraphs(document.getElementById("whole-translation"), T("翻訳しています…"));
    try {
      const { ok, payload } = await postJson(base + "/api/translate-article", { id: articleId, target });
      if (!ok || payload.error) throw new Error(payload.error || T("翻訳に失敗しました。"));
      makeParagraphs(document.getElementById("whole-translation"), payload.translation);
      status.textContent = T("翻訳しました。");
    } catch (error) {
      const output = document.getElementById("whole-translation");
      makeParagraphs(output, error.message || T("翻訳に失敗しました。"));
      if (owner) {
        const link = document.createElement("a");
        link.href = base + "/translation-settings";
        link.textContent = T("翻訳設定を確認");
        output.appendChild(link);
      }
      status.textContent = T("翻訳に失敗しました。");
    } finally {
      wholeButton.disabled = false;
    }
  });

  document.getElementById("close-selection").addEventListener("click", closePopup);
  document.getElementById("close-whole").addEventListener("click", () => {
    wholeResult.hidden = true;
  });
  document
    .getElementById("copy-original")
    .addEventListener("click", (event) =>
      copyText(document.getElementById("selected-original").textContent, event.currentTarget),
    );
  document
    .getElementById("copy-selection")
    .addEventListener("click", (event) =>
      copyText(translationText(document.getElementById("selection-translation")), event.currentTarget),
    );
  document
    .getElementById("copy-whole")
    .addEventListener("click", (event) =>
      copyText(translationText(document.getElementById("whole-translation")), event.currentTarget),
    );
  document.getElementById("pin-selection")?.addEventListener("click", () => {
    const pinned = !selectionResult.classList.contains("pinned");
    setPinned(pinned);
    try {
      localStorage.setItem(PIN_KEY, pinned ? "1" : "0");
    } catch (_) {
      /* optional */
    }
    placePopup();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closePopup();
      toolbar.hidden = true;
      return;
    }
    // Keyboard (PC): J / K = next / previous article of the outlet, B = 対訳 on/off.
    if (event.ctrlKey || event.metaKey || event.altKey || event.isComposing) return;
    if (event.target.closest?.("input, select, textarea, [contenteditable]")) return;
    const key = event.key.toLowerCase();
    const link =
      key === "j"
        ? document.querySelector('a[rel="next"]')
        : key === "k"
          ? document.querySelector('a[rel="prev"]')
          : null;
    if (link) {
      event.preventDefault();
      link.click();
    } else if (key === "b" && !biButton.disabled) {
      event.preventDefault();
      toggleBilingual(false);
    }
  });
  // The card moves with its word while the page scrolls (desktop); the toolbar just goes away.
  let placing = 0;
  const follow = () => {
    if (placing) return;
    placing = window.requestAnimationFrame(() => {
      placing = 0;
      if (!isSheet()) placePopup();
    });
  };
  window.addEventListener(
    "scroll",
    () => {
      toolbar.hidden = true;
      follow();
    },
    { passive: true },
  );
  window.addEventListener("resize", follow, { passive: true });
  narrow.addEventListener?.("change", () => placePopup());

  // Phone: drag the sheet down to close it.
  let dragStart = null;
  selectionResult.addEventListener(
    "touchstart",
    (event) => {
      if (!isSheet() || event.touches.length !== 1) return;
      if (selectionResult.scrollTop > 0 && !event.target.closest(".sheet-grip, .tr-head")) return;
      dragStart = { y: event.touches[0].clientY, moved: 0 };
      selectionResult.classList.add("dragging");
    },
    { passive: true },
  );
  selectionResult.addEventListener(
    "touchmove",
    (event) => {
      if (!dragStart) return;
      dragStart.moved = Math.max(0, event.touches[0].clientY - dragStart.y);
      selectionResult.style.transform = dragStart.moved ? `translateY(${dragStart.moved}px)` : "";
    },
    { passive: true },
  );
  const endDrag = () => {
    if (!dragStart) return;
    const moved = dragStart.moved;
    dragStart = null;
    selectionResult.classList.remove("dragging");
    if (moved > 70) closePopup();
    else selectionResult.style.transform = "";
  };
  selectionResult.addEventListener("touchend", endDrag);
  selectionResult.addEventListener("touchcancel", endDrag);
})();
