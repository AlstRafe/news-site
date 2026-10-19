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
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“‘'(])|(?<=[.!?][\"”’'])\s+(?=[A-Z\"“‘'(])|\n+")
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


# ---- counts read in Korean and Chinese directly (D-65) -------------------------------------------
# Counts of people are written in a few fixed ways ("최소 53명이 숨졌다", "至少53人死亡",
# "4軍人罹難"), so they are read from the original text: no translation, no MT drift in numbers.

_KO_KIND = (("dead", ("사망", "숨지", "숨졌", "숨진", "죽", "목숨", "희생")), ("injured", ("부상", "다치", "다쳤", "중상", "경상")),
            ("missing", ("실종",)), ("detained", ("체포", "구금", "연행", "구속")), ("displaced", ("대피", "이재민", "피란")))
_ZH_KIND = (("dead", ("死亡", "罹難", "喪生", "身亡", "遇難", "死", "殞命")), ("injured", ("受傷", "輕重傷", "重傷", "輕傷", "傷")),
            ("missing", ("失蹤", "下落不明")), ("detained", ("被捕", "逮捕", "拘留", "拘捕", "羈押")),
            ("displaced", ("撤離", "疏散", "流離失所")))
_ZH_DIGITS = {"一": 1, "兩": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_KO_COUNT = re.compile(r"(?P<pre>최소|적어도|약)?\s*(?P<num>\d[\d,]*)\s*(?P<unit>만|천)?\s*(?P<about>여)?\s*명"
                       r"(?:\s*(?P<post>이상|넘게|넘는|가량|안팎|이))?")
_KO_VAGUE = re.compile(r"(?P<word>수십|수백|수천)\s*명")
_ZH_COUNT = re.compile(r"(?P<pre>至少|最少|逾|超過|超|約|近)?\s*(?P<num>\d[\d,]*|[一兩二三四五六七八九十])\s*(?P<unit>萬|千)?"
                       r"\s*(?P<about>多|餘)?\s*(?P<who>名|人|位)?")
_ZH_VAGUE = re.compile(r"(?P<word>數十|數百|數千|上百|上千)\s*(?:名|人)")
_NATIVE_SENTENCE = re.compile(r"(?<=[.!?。！？])\s*")


def _native_kind(text: str, table) -> str:
    for kind, words in table:
        if any(word in text for word in words):
            return kind
    return ""


def native_facts(text: str, lang: str) -> list[Fact]:
    """Counts of people (COMPARED kinds) in Korean ("ko") or Chinese ("zh") text."""
    if lang not in ("ko", "zh"):
        return []
    table = _KO_KIND if lang == "ko" else _ZH_KIND
    found: list[Fact] = []
    for sentence in (s.strip() for s in _NATIVE_SENTENCE.split(text) if s.strip()):
        pattern = _KO_COUNT if lang == "ko" else _ZH_COUNT
        for match in pattern.finditer(sentence):
            raw = match.group("num")
            if lang == "zh" and not raw[0].isdigit() and (not match.group("who") or sentence[max(0, match.start("num") - 1):match.start("num")] in ("數", "幾", "上", "第")):
                continue  # a lone 一/三… is a word; 數十人 is "dozens", read below
            after = sentence[match.end():match.end() + (12 if lang == "ko" else 6)]
            before = sentence[max(0, match.start() - 8):match.start()]
            if lang == "zh":
                if re.match(r"\s*(?:年|月|日|時|點|分|歲|%|％|公里|公尺|億|元|美元)", sentence[match.end("num"):match.end("num") + 3]):
                    continue
                # "4軍人罹難": up to three characters between the count and what happened
                kind = _native_kind(after[:6], table) if match.group("who") else _native_kind(after[:5], table)
            else:
                kind = _native_kind(after, table)
            kind = kind or _native_kind(before.replace("사망자", "사망").replace("死者", "死亡"), table)
            if not kind:
                continue
            value = float(raw.replace(",", "")) if raw[0].isdigit() else float(_ZH_DIGITS[raw])
            value *= {"만": 1e4, "천": 1e3, "萬": 1e4, "千": 1e3}.get(match.group("unit") or "", 1)
            pre, post, about = match.group("pre") or "", (match.groupdict().get("post") or ""), match.group("about")
            qualifier = ("少なくとも" if pre in ("최소", "적어도", "至少", "最少") else
                         "超" if pre in ("逾", "超過", "超") or post in ("이상", "넘게", "넘는") else
                         "約" if pre in ("약", "約") or post in ("가량", "안팎") or about in ("여", "多", "餘") else
                         "近く" if pre == "近" else "")
            found.append(Fact(kind, value, _with_qualifier(_format(value) + "人", qualifier), raw, sentence))
        vague = _KO_VAGUE if lang == "ko" else _ZH_VAGUE
        for match in vague.finditer(sentence):
            kind = _native_kind(sentence[match.end():match.end() + 8], table)
            if kind:
                label = {"수십": "数十", "수백": "数百", "수천": "数千", "數十": "数十", "數百": "数百", "數千": "数千",
                         "上百": "百", "上千": "千"}[match.group("word")]
                found.append(Fact(kind, None, f"{label}人", match.group("word"), sentence))
    return found


#: Reports further apart than this are not called a disagreement (D-68).
DISPUTE_HOURS = 24


def _moment(stamp: str):
    from datetime import datetime
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def compare(members: list[dict]) -> list[dict]:
    """Group the facts of several articles by kind. ``members``: dicts with id, source, published,
    and ``english`` (headline + opening in English). Returns, in ORDER:
    ``[{"kind", "label", "compared", "differs", "rows": [{"id", "source", "published", "facts": [...]}]}]``
    keeping only kinds that at least two outlets report, or that differ."""
    per_kind: dict[str, dict[int, dict]] = {}
    for member in members:
        seen: set[tuple[str, str]] = set()
        found = facts_in(member.get("english", ""))
        if member.get("native"):  # counts of people from the outlet's own Korean/Chinese text (D-65)
            native = native_facts(member["native"], member.get("lang", ""))
            found = native + [fact for fact in found if fact.kind not in COMPARED]
        for fact in found:
            key = (fact.kind, fact.text)
            if key in seen:
                continue
            seen.add(key)
            row = per_kind.setdefault(fact.kind, {}).setdefault(member["id"], {
                "id": member["id"], "source": member.get("source") or "", "published": member.get("published", ""),
                "facts": []})
            row["facts"].append({"text": fact.text, "value": fact.value, "number": fact.number,
                                 "sentence": fact.sentence, "native": bool(member.get("native")) and fact.kind in COMPARED})
    out = []
    for kind in ORDER:
        rows = list(per_kind.get(kind, {}).values())
        outlets = {row["source"] for row in rows}
        if len(outlets) < 2:
            continue
        rows.sort(key=lambda r: (r["published"] or "", r["id"]))
        differs = False
        if kind in COMPARED:
            # Two outlets whose reports came out within DISPUTE_HOURS of each other give different
            # largest figures. Reports days apart are more likely an update — or another event.
            tops = []
            for row in rows:
                values = [f["value"] for f in row["facts"] if f["value"] is not None]
                moment = _moment(row["published"])
                if values and moment:
                    tops.append((row["source"], moment, max(values)))
            differs = any(a[0] != b[0] and a[2] != b[2] and abs((a[1] - b[1]).total_seconds()) <= DISPUTE_HOURS * 3600
                          for i, a in enumerate(tops) for b in tops[i + 1:])
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


# ---- quotes ------------------------------------------------------------------------------------

_QUOTE = re.compile(r"[“\"]([^”\"]{12,400})[”\"]|‘([^’]{12,300})’")
_QUOTE_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
QUOTE_MIN_WORDS = 4
QUOTE_SAME = 0.35  # word overlap (Jaccard of content words) for "the same statement"


@dataclass
class Quote:
    text: str       # the quoted words (English)
    sentence: str   # the English sentence around them


def quotes_in(text: str) -> list[Quote]:
    """Quoted statements (four words or more) in an English text, in order."""
    found = []
    for sentence in sentences(text):
        for match in _QUOTE.finditer(sentence):
            quoted = (match.group(1) or match.group(2) or "").strip(" ,.")
            if len(_QUOTE_WORD.findall(quoted)) >= QUOTE_MIN_WORDS:
                found.append(Quote(quoted, sentence))
    return found


def _content_words(text: str) -> set[str]:
    return {_key(w) for w in _QUOTE_WORD.findall(text) if w.lower() not in _FRAMING_STOP and len(w) > 2}


def quote_groups(members: list[dict], limit: int = 8) -> list[dict]:
    """Quotes from the articles' English text, the same statement as reported by different
    outlets put together (most outlets first): [{"outlets", "quotes": [{"id", "source",
    "published", "text", "sentence"}]}]. ``members`` as for ``compare``."""
    items = []
    for member in members:
        seen: set[str] = set()
        for quote in quotes_in(member.get("english", "")):
            if quote.text in seen:
                continue
            seen.add(quote.text)
            items.append({"id": member["id"], "source": member.get("source") or "", "published": member.get("published", ""),
                          "text": quote.text, "sentence": quote.sentence, "words": _content_words(quote.text)})
    groups: list[list[dict]] = []
    for item in items:
        best, best_score = None, 0.0
        for group in groups:
            score = max(len(item["words"] & other["words"]) / max(1, len(item["words"] | other["words"])) for other in group)
            if score > best_score:
                best, best_score = group, score
        if best is not None and best_score >= QUOTE_SAME:
            best.append(item)
        else:
            groups.append([item])
    out = []
    for group in groups:
        for item in group:
            item.pop("words", None)
        group.sort(key=lambda q: (q["published"] or "", q["id"]))
        out.append({"outlets": len({q["source"] for q in group}), "quotes": group})
    out.sort(key=lambda g: (-g["outlets"], -len(g["quotes"])))
    return out[:limit]


# ---- names -------------------------------------------------------------------------------------

_NAME_WORD = r"(?:[A-Z][a-z]+(?:[A-Z][A-Za-z]*)?(?:-[a-z]+)?|[A-Z]{2,}[a-z]*)"  # Zelensky, OpenAI, Jae-myung, NATO
#: "Iran-backed", "Saudi-led": the tail is no part of the name.
_ADJECTIVE_TAIL = re.compile(r"-(?:backed|led|based|made|born|owned|run|funded|linked|style|era|related|wide|bound|"
                             r"controlled|allied|occupied|held|affiliated|aligned|friendly|majority|speaking)$")
_NAME = re.compile(rf"(?<![A-Za-z'’])({_NAME_WORD}(?:[ -]{_NAME_WORD}){{0,2}})")
_NOT_NAMES = frozenset(
    "The A An In On At For To Of And But Or As By With From After Before Over Under Into Amid Why How What Who When "
    "Where Which This That These Those It Its He She They We I You His Her Their Our My Your Is Are Was Were Be Will "
    "Would Could Can May Might Has Have Had Not No New More Most First Last Says Said Live Watch Photos Video Analysis "
    "Ex Former Ex-PM Mr Mrs Ms Dr President Prime Minister PM "
    "Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June July August "
    "September October November December BBC Yonhap CNA AP AFP Reuters".split())


def names_in(pivot: str, with_initial: bool = False) -> list[str]:
    """Names in an English headline: runs of capitalised words ("Kim Jong Un", "Zelensky",
    "Lee Jae-myung", "OpenAI"), possessive dropped; "Iran-backed" gives Iran, "US-Japan" two names.
    A single capitalised first word is left out unless ``with_initial`` ("Missile strike…")."""
    found: list[str] = []

    def add(words: list[str]) -> None:
        words = [w for w in words if w not in _NOT_NAMES]
        name = re.sub(r"['’]s$", "", " ".join(words))
        if len(name) >= 3 and name not in found and name.lower() not in _STOP:
            found.append(name)

    for match in _NAME.finditer(pivot):
        if match.start() == 0 and " " not in match.group(1) and "-" not in match.group(1) \
                and match.group(1)[1:].islower() and not with_initial:
            continue  # "Missile strike…": capitalised because it starts the headline
        current: list[str] = []
        for token in match.group(1).split(" "):
            parts = re.split(r"-(?=[A-Z])", token)  # "US-Japan": two names
            for index, part in enumerate(parts):
                if index:
                    add(current)
                    current = []
                stripped = _ADJECTIVE_TAIL.sub("", part)
                current.append(stripped)
                if stripped != part:  # "Iran-backed Houthis": the name ends at the adjective
                    add(current)
                    current = []
        add(current)
    return found


def _name_key(name: str) -> str:
    """"Ukrainian" counts as "Ukraine", "Houthis" as "Houthi"."""
    lowered = _CANON.get(name.lower(), name.lower())
    return lowered[:-1] if lowered.endswith("s") and len(lowered) > 4 and " " not in lowered else lowered


def top_names(items: list[dict], limit: int = 15) -> list[dict]:
    """The names the most outlets and headlines mention: [{"name", "articles", "outlets": [...]}];
    ``items`` have "pivot" (English headline) and "source". "Houthis" counts as "Houthi"."""
    counts: dict[str, dict] = {}
    # A headline's first word counts only if some headline has it as a name mid-sentence.
    # Headlines In Title Case capitalise every word: their names count only if known from others.
    from newsreader.library.stories_core import is_title_case
    known = {_name_key(name) for item in items if not is_title_case(item.get("pivot") or "")
             for name in names_in(item.get("pivot") or "")}
    for item in items:
        seen: set[str] = set()
        pivot = item.get("pivot") or ""
        titled = is_title_case(pivot)
        for name in names_in(pivot, with_initial=True):
            if (titled or name not in names_in(pivot)) and _name_key(name) not in known:
                continue
            key = _name_key(name)
            if key in seen:
                continue
            seen.add(key)
            entry = counts.setdefault(key, {"forms": {}, "articles": 0, "outlets": set()})
            entry["forms"][name] = entry["forms"].get(name, 0) + 1
            entry["articles"] += 1
            entry["outlets"].add(item.get("source") or "")
    ranked = sorted(counts.values(), key=lambda e: (-len(e["outlets"]), -e["articles"]))
    out: list[dict] = []
    for entry in ranked:
        if entry["articles"] < 2:
            continue
        canonical = [form for form in entry["forms"] if _CANON.get(form.lower(), form.lower()) == form.lower()]
        forms = {form: n for form, n in entry["forms"].items() if form in canonical} or entry["forms"]
        name = max(forms.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
        # a part of a longer name already listed ("Jong Un" in "Kim Jong Un") is left out
        if any(f" {name} " in f" {other['name']} " for other in out):
            continue
        out.append({"name": name, "articles": entry["articles"], "outlets": sorted(entry["outlets"])})
        if len(out) >= limit:
            break
    return out
