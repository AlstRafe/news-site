"""Same-news clustering core: pure functions, no I/O (imported unchanged from the handover).

Source: handover reference/stories_core.py (parameters D-07). Pipeline: headline -> English pivot
-> keyword weights -> time-gated similarity -> average-link clustering.
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
_GENERIC = frozenset({"us", "uk", "eu"})  # too common in headlines to count as evidence on their own

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


def features(pivot: str) -> dict[str, float]:
    """keyword -> boost; names/acronyms (capitalised away from the sentence start) weigh 1.6x."""
    out: dict[str, float] = {}
    for index, match in enumerate(_WORD.finditer(_US.sub("US", pivot))):
        raw = match.group(0)
        word = re.sub(r"'s$", "", raw.lower().replace("’", "'")).strip("'-")
        if not word or word in _STOP or (len(word) < 2 and not word.isdigit()):
            continue
        word = _stem(_CANON.get(word, word))
        word = word[:STEM_LENGTH] if len(word) > STEM_LENGTH else word
        boosted = (raw[0].isupper() and index > 0) or (raw.isupper() and len(raw) >= 2)
        out[word] = max(out.get(word, 1.0), 1.6 if boosted else 1.0)
    return out


def parse_utc(published: str) -> datetime | None:
    try:
        return datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class Corpus:
    def __init__(self, items: dict[int, tuple[dict[str, float], datetime]]):
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
        if abs((self.times[a] - self.times[b]).total_seconds()) <= MAX_GAP_HOURS * 3600:
            va, vb = self.vectors[a], self.vectors[b]
            small, big = (va, vb) if len(va) <= len(vb) else (vb, va)
            shared = [t for t in small if t in big]
            cosine = sum(va[t] * vb[t] for t in shared) / (self.norms[a] * self.norms[b])
            # Machine-translated headlines carry extra words, diluting cosine; containment is robust to that.
            contained = sum(min(va[t], vb[t]) for t in shared) / min(self.mass[a], self.mass[b])
            score = CONTAIN_BLEND * contained + (1 - CONTAIN_BLEND) * cosine
            specific = [t for t in shared if t not in _GENERIC]
            if score >= PAIR_THRESHOLD and (len(specific) >= 2 or score >= 0.6):
                value = score
        self._cache[key] = value
        return value

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


def cluster(items: dict[int, tuple[dict[str, float], datetime]]) -> list[dict[int, float]]:
    """Average-link clustering. Returns clusters (size >= 2) as {article_id: mean similarity to the others}."""
    corpus = Corpus(items)
    members = {i: [i] for i in items}
    owner = {i: i for i in items}
    for _, a, b in corpus.candidate_pairs():
        ca, cb = owner[a], owner[b]
        if ca == cb or len(members[ca]) + len(members[cb]) > MAX_CLUSTER:
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
