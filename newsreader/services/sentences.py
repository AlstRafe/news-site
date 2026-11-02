"""Which sentence of a translation renders which sentence of the original (D-57).

Azure Translator reports, at no extra cost, the length of every source and translated sentence
(``includeSentenceLength``). translation.py keeps those lengths here, keyed by target and source
text, the moment a text is translated; the relay passes them on to test builds. Later, for any
(source, translation) pair, ``spans`` answers the sentence boundaries — or, when nothing was kept
(text translated before this existed, a provider without lengths), a split at sentence-ending
punctuation that is used only when both sides have the same number of sentences.

Lengths are returned in UTF-16 code units, what JavaScript's ``String.slice`` counts.
"""

from __future__ import annotations

import hashlib
import re

from newsreader.services.cache import get as cache_get
from newsreader.services.cache import put as cache_put

CACHE_KIND = "sentlen"
CACHE_ENTRIES = 40_000
#: Sentence ends: Western . ! ? (followed by space/quote), CJK 。！？ (anywhere).
_END = re.compile(r"(?<=[.!?])[\"'”’)\]]*\s+|(?<=[。！？])[」』”’)）]*\s*")


def _key(target: str, source: str) -> str:
    return hashlib.sha256(("sent\0" + target + "\0" + source).encode("utf-8")).hexdigest()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def remember(target: str, source: str, translation: str, src: list, tr: list) -> None:
    """Keep the sentence lengths a provider reported for one translated text."""
    if (
        isinstance(src, list)
        and isinstance(tr, list)
        and src
        and len(src) == len(tr)
        and all(isinstance(n, int) and n >= 0 for n in src + tr)
    ):
        cache_put(
            CACHE_KIND,
            _key(target, source),
            {"t": _digest(translation), "src": src, "tr": tr},
            max_entries=CACHE_ENTRIES,
        )


def reported(target: str, source: str, translation: str) -> dict | None:
    """The lengths kept for exactly this source and translation, else None."""
    value = cache_get(CACHE_KIND, _key(target, source))
    if isinstance(value, dict) and value.get("t") == _digest(translation):
        return {"src": value.get("src"), "tr": value.get("tr")}
    return None


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _as_utf16(text: str, lengths: list[int]) -> list[int] | None:
    """Provider lengths (code points or UTF-16 units) as UTF-16 units, if they add up."""
    if sum(lengths) == _utf16(text):
        return lengths
    if sum(lengths) == len(text):
        out, position = [], 0
        for n in lengths:
            out.append(_utf16(text[position : position + n]))
            position += n
        return out
    return None


def cut(text: str, lengths: list[int]) -> list[str]:
    """``text`` in pieces of the given UTF-16 lengths (which must add up to it)."""
    data, out, i = text.encode("utf-16-le"), [], 0
    for n in lengths:
        out.append(data[i * 2 : (i + n) * 2].decode("utf-16-le", "replace"))
        i += n
    return out


def utf16_lengths(pieces: list[str]) -> list[int]:
    return [_utf16(piece) for piece in pieces]


def split(text: str) -> list[str]:
    """Sentences, keeping every character (the pieces join back to ``text``)."""
    pieces, start = [], 0
    for match in _END.finditer(text):
        if match.end() > start and text[start : match.end()].strip():
            pieces.append(text[start : match.end()])
            start = match.end()
    if text[start:]:
        if pieces and not text[start:].strip():
            pieces[-1] += text[start:]
        else:
            pieces.append(text[start:])
    return pieces


def spans(target: str, source: str, translation: str) -> dict | None:
    """``{"src": [...], "tr": [...]}`` sentence lengths (UTF-16) pairing source and translation
    sentence by sentence, or None when they cannot be paired."""
    if not source or not translation:
        return None
    kept = reported(target, source, translation)
    if kept:
        src, tr = _as_utf16(source, kept["src"]), _as_utf16(translation, kept["tr"])
        if src and tr and len(src) == len(tr):
            return {"src": src, "tr": tr}
    a, b = split(source), split(translation)
    if not a or len(a) != len(b):
        return None  # one sentence each pairs trivially; different counts cannot be paired
    return {"src": [_utf16(s) for s in a], "tr": [_utf16(s) for s in b]}


def pairs(target: str, source: str, translation: str) -> list[tuple[str, str]] | None:
    """The same as ``spans``, as (source sentence, translated sentence) strings."""
    found = spans(target, source, translation)
    if not found:
        return None
    out, a, b = [], source.encode("utf-16-le"), translation.encode("utf-16-le")
    i = j = 0
    for n, m in zip(found["src"], found["tr"]):
        out.append(
            (
                a[i * 2 : (i + n) * 2].decode("utf-16-le", "replace").strip(),
                b[j * 2 : (j + m) * 2].decode("utf-16-le", "replace").strip(),
            )
        )
        i, j = i + n, j + m
    return out
