// 訳文の数字チェック (D-69): numbers in an original paragraph that its translation does not carry —
// the kind of slip machine translation makes (a dropped "53", 1.2 million read as 12万).
// Pure functions; article_translate.js shows the result under each translated paragraph.
(function (root) {
  "use strict";
  const SCALE = {
    "万": 1e4, "萬": 1e4, "만": 1e4, "億": 1e8, "억": 1e8, "千": 1e3, "천": 1e3, "兆": 1e12, "조": 1e12,
    thousand: 1e3, million: 1e6, billion: 1e9, trillion: 1e12, bn: 1e9, m: 1e6, k: 1e3,
  };
  // A figure, then an optional scale written after it ("1.2 million", "53만", "1万2000" is two parts).
  const FIGURE = /(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion|trillion|bn|[万萬만億억千천兆조])?/gi;

  function values(text) {
    const out = [];
    const source = String(text || "");
    let match;
    FIGURE.lastIndex = 0;
    while ((match = FIGURE.exec(source))) {
      const before = source.slice(Math.max(0, match.index - 1), match.index);
      if (/[A-Za-z]/.test(before)) continue; // part of a name: "G7", "COVID19"
      const raw = match[1].replace(/,/g, "").replace(/\.$/, "");
      const number = Number(raw);
      if (!Number.isFinite(number)) continue;
      const unit = (match[2] || "").toLowerCase();
      const scaled = number * (SCALE[unit] || 1);
      // "1万2000" = 12000: a small number right after a scaled one belongs to it
      const previous = out[out.length - 1];
      if (previous && previous.scaled && match.index === previous.end && scaled < previous.scale) {
        previous.value += scaled;
        previous.end = match.index + match[0].length;
        continue;
      }
      out.push({ text: match[0].trim(), value: scaled, scaled: Boolean(SCALE[unit]), scale: SCALE[unit] || 1,
                 end: match.index + match[0].length, plain: number });
    }
    return out;
  }

  const near = (a, b) => a === b || (a > 0 && Math.abs(a - b) / a < 0.001);

  // The original's figures that appear in the translation neither as the same value nor as the
  // same digits (a translation may keep "1.2 million" as 1.2 or write it as 120万: both count).
  function missing(original, translation) {
    const theirs = values(translation);
    const seen = new Set();
    const out = [];
    for (const figure of values(original)) {
      if (figure.value < 10 && !figure.scaled) continue; // small numbers are often written as words (二人, two)
      const found = theirs.some(t => near(t.value, figure.value) || near(t.plain, figure.plain) || near(t.value, figure.plain));
      if (!found && !seen.has(figure.text)) {
        seen.add(figure.text);
        out.push(figure.text);
      }
    }
    return out;
  }

  const api = { values, missing };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.NumberCheck = api;
})(typeof window !== "undefined" ? window : this);
