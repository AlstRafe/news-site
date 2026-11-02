// 単語帳: remove a word; hide the meanings to test yourself (tap a word to see its meaning).
(() => {
  "use strict";
  const T = NR.t; // a text in the page's language (i18n.js)
  const base = document.querySelector('meta[name="app-base"]')?.content || "";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const list = document.getElementById("word-list");
  if (!list) return;
  list.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-remove-word]");
    if (button) {
      button.disabled = true;
      const response = await fetch(base + "/api/words/delete", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ word_id: Number(button.dataset.removeWord) }),
      }).catch(() => null);
      if (response?.ok) button.closest("li").remove();
      else button.disabled = false;
      return;
    }
    const item = event.target.closest("li");
    if (item && list.classList.contains("quiz")) item.classList.toggle("shown");
  });
  const quiz = document.getElementById("words-quiz");
  quiz?.addEventListener("click", () => {
    const on = quiz.getAttribute("aria-pressed") !== "true";
    quiz.setAttribute("aria-pressed", String(on));
    quiz.textContent = on ? T("意味を表示") : T("意味を隠して復習");
    list.classList.toggle("quiz", on);
    list.querySelectorAll("li.shown").forEach((li) => li.classList.remove("shown"));
  });
})();
