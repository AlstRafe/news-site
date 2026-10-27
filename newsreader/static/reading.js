// Article page: 後で読む, 読み上げ and 文字の大きさ.
(() => {
  "use strict";
  const T = NR.t; // a text in the page's language (i18n.js)
  const base = document.querySelector('meta[name="app-base"]')?.content || "";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const reader = document.getElementById("reading-content");
  const post = async (url, body) => {
    const response = await fetch(base + url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify(body)
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || T("保存できませんでした。"));
    return payload;
  };

  // 後で読む: kept on the server, so what is saved on the PC is on the phone's 保存 page too.
  const save = document.getElementById("save-article");
  save?.addEventListener("click", async () => {
    const on = save.getAttribute("aria-pressed") !== "true";
    save.disabled = true;
    try {
      const payload = await post("/api/saved", { id: Number(save.dataset.id), saved: on });
      save.setAttribute("aria-pressed", String(payload.saved));
      save.textContent = payload.saved ? T("保存済み") : T("後で読む");
    } catch (error) {
      save.textContent = error.message;
    } finally {
      save.disabled = false;
    }
  });

  // 文字の大きさ: per device, applied before the page is drawn (base.html) so nothing jumps.
  const SIZE_KEY = "news-html-reader-text-size";
  const sizes = [...document.querySelectorAll("[data-text-size]")];
  const setSize = size => {
    if (size === "m") document.documentElement.removeAttribute("data-text-size");
    else document.documentElement.setAttribute("data-text-size", size);
    sizes.forEach(b => b.setAttribute("aria-pressed", String(b.dataset.textSize === size)));
    try { localStorage.setItem(SIZE_KEY, size); } catch (_) { /* optional */ }
  };
  sizes.forEach(b => b.addEventListener("click", () => setSize(b.dataset.textSize)));
  setSize(document.documentElement.getAttribute("data-text-size") || "m");

  // 読み上げ: the device's own voices (nothing is sent anywhere). In 対訳 the translation is read,
  // otherwise the original in its language; the paragraph being read is marked and kept in view.
  const aloud = document.getElementById("read-aloud");
  const synth = window.speechSynthesis;
  if (!aloud || !synth || !reader || typeof SpeechSynthesisUtterance === "undefined") return;
  aloud.hidden = false;
  let speaking = false;
  const voiceFor = lang => {
    const prefix = (lang || "ja").toLowerCase().split("-")[0];
    const voices = synth.getVoices().filter(v => v.lang.toLowerCase().replace("_", "-").startsWith(prefix));
    // Traditional Chinese text: a Taiwan voice when there is one
    if (prefix === "zh") return voices.find(v => /tw|hant/i.test(v.lang)) || voices[0] || null;
    return voices.find(v => v.localService) || voices[0] || null;
  };
  const pieces = text => text.match(/[^。！？.!?]+[。！？.!?」』"']*\s*|.+$/g) || [text];
  const stop = () => {
    speaking = false;
    synth.cancel();
    reader.querySelectorAll(".speaking").forEach(p => p.classList.remove("speaking"));
    aloud.setAttribute("aria-pressed", "false");
    aloud.textContent = T("読み上げ");
  };
  const start = () => {
    const bilingual = reader.classList.contains("bilingual") && reader.querySelector(".bi-tr");
    const paragraphs = [...reader.querySelectorAll(bilingual ? ":scope > p.bi-tr" : ":scope > p:not(.bi-tr)")]
      .filter(p => p.textContent.trim());
    if (!paragraphs.length) return;
    // start from the first paragraph on screen, so a reader halfway down is not sent back
    const first = Math.max(0, paragraphs.findIndex(p => p.getBoundingClientRect().bottom > 90));
    speaking = true;
    aloud.setAttribute("aria-pressed", "true");
    aloud.textContent = T("読み上げを止める");
    paragraphs.slice(first).forEach((p, i, list) => {
      const lang = p.lang || p.closest("[lang]")?.lang || "ja";
      const voice = voiceFor(lang);
      pieces(p.textContent.trim()).forEach((text, j, all) => {
        const u = new SpeechSynthesisUtterance(text);
        u.lang = voice?.lang || lang;
        if (voice) u.voice = voice;
        if (j === 0) u.onstart = () => {
          reader.querySelectorAll(".speaking").forEach(n => n.classList.remove("speaking"));
          p.classList.add("speaking");
          const r = p.getBoundingClientRect();
          if (r.top < 70 || r.bottom > innerHeight - 60) p.scrollIntoView({ behavior: "smooth", block: "center" });
        };
        if (i === list.length - 1 && j === all.length - 1) u.onend = () => { if (speaking) stop(); };
        synth.speak(u);
      });
    });
  };
  aloud.addEventListener("click", () => (speaking ? stop() : start()));
  addEventListener("pagehide", () => synth.cancel());
})();
