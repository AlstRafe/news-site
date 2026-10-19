"""読み比べの「違いを見る」: what outlets report differently about the same news (D-56).

Pure functions over English text (an English outlet's own words, or other outlets' text
translated to English once and cached by the caller); nothing here translates or reads files.

1. Numbers (``facts``): figures in each article's headline and opening, sorted into kinds
   (死者, 負傷者, 行方不明, 拘束, 避難, 割合, 金額, 人数, 年齢). For counts of people
   (死者 … 避難) the outlets' figures are compared and a difference is flagged — the classic
   "BBC says 50 dead, Yonhap says at least 53" — with each report's time beside it, since a
   later report often carries an updated figure.
2. Words (``framing``): the words of the English headlines that only one outlet uses, and the
   ones every outlet uses — how each frames the same event.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from newsreader.library.stories_core import _CANON, _STOP, _stem

# ---- numbers -----------------------------------------------------------------------------------

_SMALL = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
          "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
          "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100}
_VAGUE = {"dozens": "数十", "hundreds": "数百", "thousands": "数千", "scores": "数十", "a dozen": "十数"}
_SCALE = {"thousand": 1e3, "million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "trillion": 1e12, "tn": 1e12}
_QUALIFIERS = (("at least", "少なくとも"), ("more than", "超"), ("over", "超"), ("nearly", "近く"),
               ("almost", "近く"), ("about", "約"), ("around", "約"), ("approximately", "約"),
               ("roughly", "約"), ("some", "約"), ("up to", "最大"), ("fewer than", "未満"), ("less than", "未満"))
_CURRENCY_PREFIX = {"us$": "ドル", "nt$": "台湾ドル", "$": "ドル", "€": "ユーロ", "£": "ポンド", "¥": "円", "₩": "ウォン"}
_CURRENCY_WORD = {"dollars": "ドル", "dollar": "ドル", "won": "ウォン", "yuan": "元", "yen": "円", "euros": "ユーロ",
                  "euro": "ユーロ", "pounds": "ポンド", "rupees": "ルピー", "rubles": "ルーブル", "roubles": "ルーブル",
                  "dinars": "ディナール", "pesos": "ペソ", "reais": "レアル", "shekels": "シェケル", "baht": "バーツ",
                  "rupiah": "ルピア", "francs": "フラン", "hryvnias": "フリヴニャ", "ringgit": "リンギット"}

#: kind -> (label, words that, near a number, say what is counted)
_KINDS = {
    "dead": ("死者", ("killed", "kills", "kill", "dead", "died", "deaths", "death", "toll", "fatalities", "lives",
                    "bodies", "killing")),
    "injured": ("負傷者", ("injured", "injuring", "wounded", "wounding", "hurt", "injuries", "casualties")),
    "missing": ("行方不明", ("missing",)),
    "detained": ("拘束・逮捕", ("arrested", "detained", "arrests", "detentions", "custody")),
    "displaced": ("避難", ("evacuated", "displaced", "evacuees", "homeless", "fled")),
}
_PEOPLE = ("people", "persons", "residents", "citizens", "soldiers", "troops", "protesters", "workers", "children",
           "students", "passengers", "civilians", "employees", "members", "voters", "nationals", "migrants",
           "patients", "staff", "police", "officers", "fighters", "hostages", "prisoners", "votes")
#: Counts of people that are compared between outlets (others are shown side by side only).
COMPARED = ("dead", "injured", "missing", "detained", "displaced")
LABELS = {kind: label for kind, (label, _) in _KINDS.items()} | {
    "percent": "割合", "money": "金額", "people": "人数", "age": "年齢"}
ORDER = ("dead", "injured", "missing", "detained", "displaced", "people", "money", "percent", "age")

_NUMBER = re.compile(
    r"(?P<cur>US\$|NT\$|\$|€|£|¥|₩)?\s?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<scale>thousand|million|billion|trillion|bn|tn|m)\b)?"
    r"(?P<pct>\s?%|\s?percent\b|\s?per cent\b|\s?percentage points?\b|%p)?"
    r"(?P<yo>-year-old|\s?years? old)?",
    re.IGNORECASE)
_WORDNUM = re.compile(r"\b(?P<word>" + "|".join(sorted(_SMALL, key=len, reverse=True))
                      + r"|dozens|hundreds|thousands|scores|a dozen)\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"”’']?\s+(?=[A-Z\"“‘'(])|\n+")
_YEAR = re.compile(r"^(19|20)\d\d$")


@dataclass
class Fact:
    kind: str
    value: float | None   # None: "dozens", "hundreds" …
    text: str             # as shown: "少なくとも 53", "数十", "1.9兆ドル"
    number: str           # the figure as written in English, for highlighting: "53", "1.9"
    sentence: str         # the English sentence it came from


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text) if len(s.strip()) > 2]


def _qualifier(before: str) -> str:
    tail = before.lower().rstrip()
    for english, japanese in _QUALIFIERS:
        if tail.endswith(english):
            return japanese
    return ""


_KIND_OF_WORD = {word: kind for kind, (_, words) in _KINDS.items() for word in words}
WINDOW = 5


def _kind(before: str, after: str, *, percent: bool, currency: str, age: bool) -> str:
    """What a number counts: the nearest counting word within WINDOW words on either side
    ("killed 5 people and injured 20": 5 is dead, 20 injured), a word after winning a tie."""
    if age:
        return "age"
    if percent:
        return "percent"
    if currency:
        return "money"
    words_after = re.findall(r"[a-z]+", after.lower())[:WINDOW]
    words_before = re.findall(r"[a-z]+", before.lower())[-WINDOW:]
    best: tuple[float, str] | None = None
    for distance, word in enumerate(words_after, start=1):
        if word in ("killing", "injuring", "wounding"):
            continue  # "killing 50 and injuring about 200": an -ing verb counts the number after it
        if word in _KIND_OF_WORD and (best is None or distance - 0.5 < best[0]):
            best = (distance - 0.5, _KIND_OF_WORD[word])
    for distance, word in enumerate(reversed(words_before), start=1):
        if word in _KIND_OF_WORD and (best is None or distance < best[0]):
            best = (distance, _KIND_OF_WORD[word])
    if best:
        return best[1]
    if any(word in _PEOPLE for word in words_after[:2]):
        return "people"
    return ""


def _format(value: float) -> str:
    for size, suffix in ((1e12, "兆"), (1e8, "億"), (1e4, "万")):
        if value >= size:
            shown = value / size
            return (f"{shown:,.1f}".rstrip("0").rstrip(".")) + suffix
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}".rstrip("0").rstrip(".")


def _unit(kind: str) -> str:
    return "人" if kind in COMPARED or kind == "people" else ""


def _with_qualifier(shown: str, qualifier: str) -> str:
    """少なくとも 20人 / 約 200人 / 最大 5人, but 50人超 / 30人近く / 10人未満."""
    if qualifier in ("少なくとも", "約", "最大"):
        return f"{qualifier} {shown}"
    return f"{shown}{qualifier}" if qualifier else shown


def facts_in(text: str) -> list[Fact]:
    """The figures of known kinds in an English text, in order."""
    found: list[Fact] = []
    for sentence in sentences(text):
        taken: list[tuple[int, int]] = []
        for match in _NUMBER.finditer(sentence):
            raw = match.group("num")
            start, end = match.start("cur") if match.group("cur") else match.start("num"), match.end()
            if start > 0 and (sentence[start - 1].isalnum() or sentence[start - 1] in ":/.,"):
                continue  # part of a word, a time or a date
            if end < len(sentence) and sentence[end].isalnum():
                continue  # "19th", "5G", "G7"
            value = float(raw.replace(",", ""))
            scale = (match.group("scale") or "").lower()
            percent = bool(match.group("pct"))
            age = bool(match.group("yo")) or bool(re.search(r"\baged\s*$", sentence[:start], re.IGNORECASE))
            after = sentence[end:end + 60]
            currency = _CURRENCY_PREFIX.get((match.group("cur") or "").lower(), "")
            word_currency = re.match(r"\s*(?:[A-Za-z]+\s)?(" + "|".join(_CURRENCY_WORD) + r")\b", after, re.IGNORECASE)
            if not currency and word_currency:
                currency = _CURRENCY_WORD.get(word_currency.group(1).lower(), "")
            if scale == "m" and not currency:
                scale = ""  # "5m" without money is more likely metres
            if _YEAR.match(raw) and not (scale or percent or currency or age):
                continue
            if re.match(r"\s*(?:a\.m\.|p\.m\.|am\b|pm\b|GMT|BST|KST|km|kg|cm|mm|ft|metres|meters|miles|km/h)",
                        after, re.IGNORECASE):
                continue
            value *= _SCALE.get(scale, 1)
            kind = _kind(sentence[:start], after, percent=percent, currency=currency, age=age)
            if not kind:
                continue
            qualifier = _qualifier(sentence[:start])
            points = percent and ("point" in match.group("pct").lower() or match.group("pct").lower() == "%p")
            shown = (f"{float(raw.replace(',', '')):g}ポイント" if points else
                     f"{float(raw.replace(',', '')):g}%" if percent else
                     _format(value) + currency if currency else
                     f"{_format(value)}歳" if age else _format(value) + _unit(kind))
            found.append(Fact(kind, value, _with_qualifier(shown, qualifier), raw, sentence))
            taken.append((start, end))
        for match in _WORDNUM.finditer(sentence):
            start, end = match.span()
            if any(s <= start < e for s, e in taken):
                continue
            word = match.group("word").lower()
            after = sentence[end:end + 60]
            if re.match(r"\s*(?:of\s)?(?:years?|days?|weeks?|months?|hours?|minutes?|times|percent|%)\b", after,
                        re.IGNORECASE):
                continue
            kind = _kind(sentence[:start], after, percent=False, currency="", age=False)
            if kind not in COMPARED and kind != "people":
                continue
            value = float(_SMALL[word]) if word in _SMALL else None
            qualifier = _qualifier(sentence[:start])
            shown = (_VAGUE.get(word) or _format(value or 0)) + _unit(kind)
            found.append(Fact(kind, value, _with_qualifier(shown, qualifier), match.group("word"), sentence))
    return found


def compare(members: list[dict]) -> list[dict]:
    """Group the facts of several articles by kind. ``members``: dicts with id, source, published,
    and ``english`` (headline + opening in English). Returns, in ORDER:
    ``[{"kind", "label", "compared", "differs", "rows": [{"id", "source", "published", "facts": [...]}]}]``
    keeping only kinds that at least two outlets report, or that differ."""
    per_kind: dict[str, dict[int, dict]] = {}
    for member in members:
        seen: set[tuple[str, str]] = set()
        for fact in facts_in(member.get("english", "")):
            key = (fact.kind, fact.text)
            if key in seen:
                continue
            seen.add(key)
            row = per_kind.setdefault(fact.kind, {}).setdefault(member["id"], {
                "id": member["id"], "source": member.get("source") or "", "published": member.get("published", ""),
                "facts": []})
            row["facts"].append({"text": fact.text, "value": fact.value, "number": fact.number,
                                 "sentence": fact.sentence})
    out = []
    for kind in ORDER:
        rows = list(per_kind.get(kind, {}).values())
        outlets = {row["source"] for row in rows}
        if len(outlets) < 2:
            continue
        rows.sort(key=lambda r: (r["published"] or "", r["id"]))
        differs = False
        if kind in COMPARED:
            # each outlet's largest exact figure; outlets that disagree on it are flagged
            tops = {}
            for row in rows:
                values = [f["value"] for f in row["facts"] if f["value"] is not None]
                if values:
                    tops.setdefault(row["source"], set()).add(max(values))
            distinct = {v for values in tops.values() for v in values}
            differs = len(tops) >= 2 and len(distinct) > 1
        out.append({"kind": kind, "label": LABELS[kind], "compared": kind in COMPARED, "differs": differs,
                    "rows": rows})
    return out


# ---- words -------------------------------------------------------------------------------------

_DISPLAY_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
_FRAMING_STOP = _STOP | frozenset("s us uk eu says said report reports reported amid".split())


def _key(word: str) -> str:
    lowered = word.lower().replace("’", "'")
    lowered = re.sub(r"'s$", "", lowered)
    stem = _stem(_CANON.get(lowered, lowered))
    return _CANON.get(stem, stem)  # "kills" -> "kill" -> "dead", like "killed"


def framing(members: list[dict], limit: int = 6) -> dict:
    """Words of the English headlines (``pivot``) per outlet: ``{"shared": [words every outlet
    uses], "only": {source: [words only this outlet uses]}}``; each word as most often written."""
    surface: dict[str, dict[str, int]] = {}
    by_outlet: dict[str, dict[str, int]] = {}
    for member in members:
        source = member.get("source") or "未分類"
        pivot = member.get("pivot") or ""
        for raw in _DISPLAY_WORD.findall(pivot):
            word = raw.strip("'-’")
            lowered = word.lower()
            if len(lowered) < 3 or lowered in _FRAMING_STOP:
                continue
            key = _key(word)
            if key in _FRAMING_STOP or len(key) < 3:
                continue
            surface.setdefault(key, {})
            surface[key][word] = surface[key].get(word, 0) + 1
            by_outlet.setdefault(source, {})
            by_outlet[source][key] = by_outlet[source].get(key, 0) + 1
    if len(by_outlet) < 2:
        return {"shared": [], "only": {}}

    def shown(key: str) -> str:
        """The most frequent spelling; lower case when the word is ever written so (not a name)."""
        forms = surface[key]
        best = max(forms.items(), key=lambda kv: (kv[1], kv[0]))[0]
        return best.lower() if any(form.islower() for form in forms) and not best.isupper() else best

    keys_per_outlet = {source: set(words) for source, words in by_outlet.items()}
    shared_keys = set.intersection(*keys_per_outlet.values())
    total = {key: sum(words.get(key, 0) for words in by_outlet.values()) for key in surface}
    shared = [shown(k) for k in sorted(shared_keys, key=lambda k: (-total[k], k))][:limit]
    only = {}
    for source, words in sorted(by_outlet.items(), key=lambda kv: kv[0].casefold()):
        others = set().union(*(keys for s, keys in keys_per_outlet.items() if s != source))
        mine = [k for k in words if k not in others]
        mine.sort(key=lambda k: (-words[k], k))
        only[source] = [shown(k) for k in mine[:limit]]
    return {"shared": shared, "only": only}
