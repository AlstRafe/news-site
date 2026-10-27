/* NR.t: a text of the scripts in the page's language (newsreader/i18n.py). Texts are written in
   Japanese and looked up by it; /i18n/<lang>.js, loaded before this file, brings the translations
   (none for Japanese), and a text without one stays Japanese. NR.t("{n} 件", { n: 3 }) fills in {n};
   a translation {one, other} is picked by the number (count or n). */
(() => {
  "use strict";
  const texts = window.NR_TEXTS || {};
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  window.NR = window.NR || {};
  NR.lang = document.documentElement.lang || "ja";
  // The translation target that reads as the page's language: 翻訳先 until the reader picks another
  NR.target = document.querySelector('meta[name="translation-default"]')?.content || "ja";
  NR.t = (ja, values) => {
    let said = own(texts, ja) ? texts[ja] : ja;
    if (said && typeof said === "object") said = said[values && (values.count ?? values.n) === 1 ? "one" : "other"] || ja;
    return values ? String(said).replace(/\{(\w+)\}/g, (hole, key) => (own(values, key) ? values[key] : hole)) : said;
  };
})();
