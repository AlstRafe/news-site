"""Same-news clustering core: pure functions, no I/O.

Source: handover reference/stories_core.py (parameters D-07). Pipeline: headline -> English pivot
-> keyword weights -> time-gated similarity -> average-link clustering.

One addition (D-21): a pair that scores below ``NAME_EVIDENCE_BELOW`` must share at least one
*name-like* token — a word capitalised away from the start of some headline, a word never seen in
lower case in the corpus, or a one/two-word quoted term ('cockroach' party). Shared common words
alone ("data centre", "school pupils", "protests turn") no longer join different events. A Title
Case headline (machine translation sometimes produces them) says nothing about which words are
names, so its capitals are ignored. Without ``shapes`` (the handover's call signature) the
behaviour is exactly D-07.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timezone

MAX_GAP_HOURS = 72
CONTAIN_BLEND = 0.5
PAIR_THRESHOLD = 0.25
AVG_LINK_THRESHOLD = 0.12
MAX_CLUSTER = 12
COMMON_SHARE = 0.25
STEM_LENGTH = 6
_GENERIC = frozenset({"us", "uk", "eu"})
_NUMBER_WORDS = frozenset("one two three four five six seven eight nine ten dozen hundred thousand million".split())  # too common in headlines to count as evidence on their own

_STOP = frozenset(
    "a an the and or but of to in on for at by with from as is are was were be been being it its this that "
    "these those after before over under into amid says say said new more than not no will would could can "
    "may might has have had his her their he she they we you i about up out off who what when where why how "
    "also just now still after again all one two three first last year years day days week".split()
)
_CANON = {
    "ukrainian": "ukraine", "ukrainians": "ukraine", "russian": "russia", "russians": "russia", "iranian": "iran",
    "yemeni": "yemen", "polish": "poland", "indian": "india", "french": "france", "chinese": "china",
    "japanese": "japan", "israeli": "israel", "palestinian": "palestine", "german": "germany",
    "taiwanese": "taiwan", "european": "eu", "british": "uk", "britain": "uk", "american": "us", "americans": "us",
    "saudis": "saudi", "korean": "korea", "philippine": "philippines",
    "premier": "pm", "prime": "pm",
    "strike": "attack", "struck": "attack", "attacked": "attack", "hit": "attack", "assault": "attack",
    "kill": "dead", "killed": "dead", "death": "dead", "died": "dead", "dies": "dead", "die": "dead",
    "arrest": "detain", "arrested": "detain", "detained": "detain", "held": "detain",
    "protester": "protest", "demonstration": "protest", "demonstrator": "protest", "rally": "protest",
    "truce": "ceasefire",
}
_US = re.compile(r"\bU\.S\.A?\.?|\bUnited States\b|\bAmerica(?:n|ns)?\b")
_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*|\d[\d.,]*\d|\d")
_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ]")
_KANA = re.compile(r"[぀-ヿ]")
_HAN = re.compile(r"[㐀-䶿一-鿿]")
_LATIN = re.compile(r"[A-Za-z]")
NAME_EVIDENCE_BELOW = 0.6
# A one/two-word quoted term: 'cockroach' party, ‘energy ceasefire’ (not quoted speech).
_QUOTED = re.compile(r"(?:^|(?<=[\s(]))['‘\"“]([^'’\"”]{2,40})['’\"”](?=$|[\s,.:;!?)]|'s)")


def detect_lang(text: str) -> str:
    hangul, kana, han, latin = (len(r.findall(text)) for r in (_HANGUL, _KANA, _HAN, _LATIN))
    letters = hangul + kana + han + latin
    if not letters:
        return "other"
    if hangul / letters >= 0.2:
        return "ko"
    if kana:
        return "ja"
    if han / letters >= 0.3:
        return "zh"
    return "en" if latin / letters >= 0.5 else "other"


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    return word


def _tokens(pivot: str):
    """(token, boosted, shape) per keyword; shape: quoted / name / initial / lower / digit."""
    text = _US.sub("US", pivot)
    quoted: set[int] = set()
    for match in _QUOTED.finditer(text):
        if 1 <= len(match.group(1).split()) <= 2:
            quoted.update(range(match.start(1), match.end(1)))
    for index, match in enumerate(_WORD.finditer(text)):
        raw = match.group(0)
        word = re.sub(r"'s$", "", raw.lower().replace("’", "'")).strip("'-")
        # Hyphenated: names keep their parts ("Iran-backed" -> iran, backed; "Ex-OpenAI" -> ex,
        # openai; "US-Japan"); plain compounds are one word ("run-off" = "runoff", "tight-lipped").
        parts = [part for part in word.split("-") if part]
        named = any(piece[:1].isupper() for piece in raw.split("-")[1:]) or (index > 0 and raw[:1].isupper())
        words = parts if named else ["".join(parts)]
        boosted = (raw[0].isupper() and index > 0) or (raw.isupper() and len(raw) >= 2)
        if match.start() in quoted:
            shape = "quoted"
        elif boosted:
            shape = "name"
        elif raw[0].isupper():
            shape = "initial"
        elif raw[0].isdigit():
            shape = "digit"
        else:
            shape = "lower"
        for word in words:
            if not word or word in _STOP or (len(word) < 2 and not word.isdigit()):
                continue
            word = _stem(_CANON.get(word, word))
            word = word[:STEM_LENGTH] if len(word) > STEM_LENGTH else word
            yield word, boosted, shape


def features(pivot: str) -> dict[str, float]:
    """keyword -> boost; names/acronyms (capitalised away from the sentence start) weigh 1.6x."""
    out: dict[str, float] = {}
    for word, boosted, _ in _tokens(pivot):
        out[word] = max(out.get(word, 1.0), 1.6 if boosted else 1.0)
    return out


def is_title_case(pivot: str) -> bool:
    """"Man Killed After Attacking Tiger Zone": every content word (4+ letters) after the first is capitalised."""
    words = [w for w in _WORD.findall(pivot)[1:] if len(w) >= 4 and w[0].isalpha() and w.lower() not in _STOP]
    return len(words) >= 3 and sum(w[0].isupper() for w in words) >= 0.9 * len(words)


def shapes(pivot: str) -> dict[str, frozenset[str]]:
    """keyword -> the shapes it takes in this headline (input to ``name_tokens``)."""
    title_case = is_title_case(pivot)
    out: dict[str, set[str]] = defaultdict(set)
    for word, _, shape in _tokens(pivot):
        if title_case and shape in ("name", "initial"):
            shape = "styled"  # capitalised because of the style: evidence neither way
        out[word].add(shape)
    return {word: frozenset(found) for word, found in out.items()}


def name_tokens(per_item: dict[int, dict[str, frozenset[str]]]) -> set[str]:
    """Corpus-wide: capitalised mid-headline / quoted somewhere, or capitalised but never lower case."""
    seen: dict[str, set[str]] = defaultdict(set)
    for item in per_item.values():
        for word, found in item.items():
            seen[word] |= found
    return {word for word, found in seen.items()
            if "name" in found or "quoted" in found or ("initial" in found and "lower" not in found)}


def parse_utc(published: str) -> datetime | None:
    try:
        return datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class Corpus:
    def __init__(self, items: dict[int, tuple[dict[str, float], datetime]], names: set[str] | None = None,
                 forbidden: set[tuple[int, int]] | None = None):
        self.names = names
        self.forbidden = forbidden or set()
        self.times = {i: t for i, (_, t) in items.items()}
        doc_freq: dict[str, int] = defaultdict(int)
        for feats, _ in items.values():
            for token in feats:
                doc_freq[token] += 1
        self.doc_freq, self.n = doc_freq, max(1, len(items))
        self.vectors, self.norms, self.mass = {}, {}, {}
        for i, (feats, _) in items.items():
            vec = {t: (math.log((self.n + 1) / (doc_freq[t] + 1)) + 1.0) * boost for t, boost in feats.items()}
            self.vectors[i] = vec
            self.norms[i] = math.sqrt(sum(w * w for w in vec.values())) or 1.0
            self.mass[i] = sum(vec.values()) or 1.0
        self._cache: dict[tuple[int, int], float] = {}

    def sim(self, a: int, b: int) -> float:
        key = (a, b) if a < b else (b, a)
        if key in self._cache:
            return self._cache[key]
        value = 0.0
        if key in self.forbidden:  # the reader said these are different news
            self._cache[key] = value
            return value
        if abs((self.times[a] - self.times[b]).total_seconds()) <= MAX_GAP_HOURS * 3600:
            va, vb = self.vectors[a], self.vectors[b]
            small, big = (va, vb) if len(va) <= len(vb) else (vb, va)
            shared = [t for t in small if t in big]
            cosine = sum(va[t] * vb[t] for t in shared) / (self.norms[a] * self.norms[b])
            # Machine-translated headlines carry extra words, diluting cosine; containment is robust to that.
            contained = sum(min(va[t], vb[t]) for t in shared) / min(self.mass[a], self.mass[b])
            score = CONTAIN_BLEND * contained + (1 - CONTAIN_BLEND) * cosine
            specific = [t for t in shared if t not in _GENERIC]
            accepted = score >= PAIR_THRESHOLD and (len(specific) >= 2 or score >= 0.6)
            if accepted and self.names is not None and score < NAME_EVIDENCE_BELOW:
                accepted = any(token in self.names for token in specific)
            if accepted:
                value = score
        self._cache[key] = value
        return value

    def raw(self, a: int, b: int) -> float:
        """The blended similarity of two headlines, without the acceptance rules (for "似た記事")."""
        va, vb = self.vectors[a], self.vectors[b]
        # Numbers ("4", "four") and generic words are no evidence; two shared words at least.
        shared = [t for t in va if t in vb and t not in _GENERIC and not t[:1].isdigit() and t not in _NUMBER_WORDS]
        if len(shared) < 2:
            return 0.0
        cosine = sum(va[t] * vb[t] for t in shared) / (self.norms[a] * self.norms[b])
        contained = sum(min(va[t], vb[t]) for t in shared) / min(self.mass[a], self.mass[b])
        return CONTAIN_BLEND * contained + (1 - CONTAIN_BLEND) * cosine

    def candidate_pairs(self) -> list[tuple[float, int, int]]:
        postings: dict[str, list[int]] = defaultdict(list)
        limit = max(8, int(self.n * COMMON_SHARE))
        for i, vec in self.vectors.items():
            for token in vec:
                if self.doc_freq[token] <= limit:
                    postings[token].append(i)
        seen: set[tuple[int, int]] = set()
        pairs = []
        for ids in postings.values():
            for x in range(len(ids)):
                for y in range(x + 1, len(ids)):
                    key = (min(ids[x], ids[y]), max(ids[x], ids[y]))
                    if key not in seen:
                        seen.add(key)
                        score = self.sim(*key)
                        if score > 0:
                            pairs.append((score, *key))
        return sorted(pairs, reverse=True)


def cluster(items: dict[int, tuple[dict[str, float], datetime]],
            shapes_by_item: dict[int, dict[str, frozenset[str]]] | None = None,
            forbidden: set[tuple[int, int]] | None = None) -> list[dict[int, float]]:
    """Average-link clustering. Returns clusters (size >= 2) as {article_id: mean similarity to the others}.

    With ``shapes_by_item`` (see ``shapes``) weak pairs need a shared name-like token (D-21).
    ``forbidden`` pairs (smaller id first) never end up in one cluster (the reader's corrections)."""
    forbidden = forbidden or set()
    corpus = Corpus(items, name_tokens(shapes_by_item) if shapes_by_item is not None else None, forbidden)
    members = {i: [i] for i in items}
    owner = {i: i for i in items}
    for _, a, b in corpus.candidate_pairs():
        ca, cb = owner[a], owner[b]
        if ca == cb or len(members[ca]) + len(members[cb]) > MAX_CLUSTER:
            continue
        if forbidden and any((min(x, y), max(x, y)) in forbidden for x in members[ca] for y in members[cb]):
            continue
        link = sum(corpus.sim(x, y) for x in members[ca] for y in members[cb]) / (len(members[ca]) * len(members[cb]))
        if link < AVG_LINK_THRESHOLD:
            continue
        for article in members[cb]:
            owner[article] = ca
        members[ca].extend(members.pop(cb))
    return [
        {i: sum(corpus.sim(i, j) for j in g if j != i) / (len(g) - 1) for i in g}
        for g in members.values() if len(g) > 1
    ]
