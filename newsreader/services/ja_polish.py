"""Japanese output of the translation engine, made to read like one written article.

Azure Translator translates each sentence on its own, so a Japanese article comes back mixing
the newspaper style (〜した、〜である) with the polite one (〜しました、〜です), sometimes in the
same paragraph, with Western spaces after 。 and half-width brackets and quotes around Japanese.
``polish`` puts it into the newspaper style a Japanese reader expects from news, and fixes the
typography. What people are quoted as saying (inside 「」 and 『』) is left as they said it.

The style change needs a morphological analyser (Janome, pure Python): ``ませんでした`` is
``なかった``, ``書きました`` is ``書いた`` but ``起きました`` is ``起きた``. Without it only the
typography is fixed. Every function returns its input unchanged when unsure.
"""
from __future__ import annotations

import re
import threading

#: Japanese letters and the punctuation that sits flush against them.
_JA = "々〆〇぀-ヿ㐀-䶿一-鿿豈-﫿、。「-』（）！？・"
_SPACE_BETWEEN = re.compile(rf"(?<=[{_JA}])[ 　\t]+(?=[{_JA}])")
_SPACE_AFTER_PUNCT = re.compile(r"(?<=[、。，！？」』）])[ 　\t]+")
_SPACE_ASCII_JA = re.compile(rf"(?<=[A-Za-z0-9%])[ \t]+(?=[{_JA}])|(?<=[{_JA}])[ \t]+(?=[A-Za-z0-9])")
_HAS_JA = re.compile(r"[぀-ヿ㐀-鿿]")
_PARENS = re.compile(r"\(([^()]*)\)")
_QUOTES = re.compile(r"\"([^\"\n]+)\"|“([^“”\n]+)”")
_COMMA = re.compile(rf"(?<=[{_JA}]),\s*(?=[{_JA}])")
_WIDE_ALNUM = str.maketrans({chr(c): chr(c - 0xFEE0) for c in [*range(0xFF10, 0xFF1A), *range(0xFF21, 0xFF3B), *range(0xFF41, 0xFF5B)]})
#: Polite forms a sentence may still contain; nothing to do for the analyser without them.
_POLITE = re.compile("ま[すしせ]|で[すし]")


def typography(text: str) -> str:
    """Spacing, brackets and quotes as Japanese is printed."""
    if not _HAS_JA.search(text):
        return text
    text = text.translate(_WIDE_ALNUM)
    text = _PARENS.sub(lambda m: f"（{m.group(1)}）" if _HAS_JA.search(m.group(1)) else m.group(0), text)
    text = _QUOTES.sub(lambda m: (f"「{m.group(1) or m.group(2)}」" if _HAS_JA.search(m.group(1) or m.group(2))
                                  else m.group(0)), text)
    text = _COMMA.sub("、", text)
    text = _SPACE_AFTER_PUNCT.sub("", text)
    text = _SPACE_BETWEEN.sub("", text)
    return _SPACE_ASCII_JA.sub("", text)


# ---- 文体（です・ます → だ・である） -------------------------------------------------

_tokenizer = None
_tokenizer_lock = threading.Lock()
_UNAVAILABLE = object()


def _analyser():
    global _tokenizer
    if _tokenizer is None:
        with _tokenizer_lock:
            if _tokenizer is None:
                try:
                    from janome.tokenizer import Tokenizer
                    _tokenizer = Tokenizer()
                except Exception:  # noqa: BLE001 - not installed / broken: typography only
                    _tokenizer = _UNAVAILABLE
    return None if _tokenizer is _UNAVAILABLE else _tokenizer


#: Godan verbs: the vowel row a dictionary form's last kana moves to before ない.
_A_ROW = dict(zip("くぐすつぬぶむるう", "かがさたなばまらわ"))


def _past(token) -> str | None:
    """``書き`` (連用形 before ました) -> ``書いた``; None when the verb is not understood."""
    kind, base, surface = token.infl_type, token.base_form, token.surface
    if kind.startswith(("一段", "サ変", "カ変")):
        return surface + "た"
    if not kind.startswith("五段") or len(base) < 1:
        return None
    stem = base[:-1]
    if "カ行促音便" in kind:
        return stem + "った"
    if "カ行イ音便" in kind:
        return stem + "いた"
    if "ガ行" in kind:
        return stem + "いだ"
    if "サ行" in kind:
        return stem + "した"
    if "ワ行ウ音便" in kind:
        return stem + "うた"
    if any(row in kind for row in ("タ行", "ラ行", "ワ行")):
        return stem + "った"
    if any(row in kind for row in ("ナ行", "バ行", "マ行")):
        return stem + "んだ"
    return None


def _negative(token) -> str | None:
    """``行き`` (連用形 before ません) -> ``行かない``; ``あり`` -> ``ない``."""
    kind, base, surface = token.infl_type, token.base_form, token.surface
    if base == "ある":
        return "ない"
    if kind.startswith("一段") or kind.startswith("サ変・−ズル"):
        return surface + "ない"
    if kind.startswith("サ変"):
        return surface[:-1] + "しない" if surface.endswith("し") else None
    if kind.startswith("カ変"):
        return ("来" if surface == "来" else "こ") + "ない"
    if kind.startswith("五段") and base and base[-1] in _A_ROW:
        return base[:-1] + _A_ROW[base[-1]] + "ない"
    return None


def _plain(token, before) -> str | None:
    """``し`` (連用形 before ます) -> ``する``; ``おり`` after て -> ``いる``."""
    base = token.base_form
    if base in ("*", "") or token.infl_type == "*":
        return None
    if base == "おる" and before is not None and before.surface in ("て", "で"):
        return "いる"
    return base


def _is(token, pos: str, kind: str = "", surface: str = "") -> bool:
    return (token is not None and token.part_of_speech.startswith(pos) and token.infl_type.startswith(kind)
            and (not surface or token.surface == surface))


def _restyle(tokens: list, depth: int) -> tuple[str, int]:
    """Tokens of one sentence in the plain style; returns the text and the quote depth after it."""
    out: list[str] = []
    i, n = 0, len(tokens)
    at = lambda k: tokens[k] if 0 <= k < n else None  # noqa: E731
    while i < n:
        token = tokens[i]
        nxt, nxt2, nxt3, nxt4 = at(i + 1), at(i + 2), at(i + 3), at(i + 4)
        if depth == 0 and token.infl_form.startswith("連用") and _is(nxt, "助動詞", "特殊・マス"):
            before = at(i - 1)
            if nxt.surface == "ます":
                plain = _plain(token, before)
                if plain:
                    out.append(plain)
                    i += 2
                    continue
            elif nxt.surface == "まし" and _is(nxt2, "助動詞", "特殊・タ"):
                if token.base_form == "おる" and before is not None and before.surface in ("て", "で"):
                    out.append("いた")
                    i += 3
                    continue
                past = _past(token)
                if past:
                    out.append(past)
                    i += 3
                    continue
            elif nxt.surface == "ませ" and _is(nxt2, "助動詞", surface="ん"):
                negative = _negative(token)
                if negative:
                    if _is(nxt3, "助動詞", "特殊・デス", "でし") and _is(nxt4, "助動詞", "特殊・タ"):
                        out.append(negative[:-1] + "かった")
                        i += 5
                    else:
                        out.append(negative)
                        i += 3
                    continue
        if depth == 0 and _is(token, "助動詞", "特殊・デス"):
            before = at(i - 1)
            after_adjective = before is not None and (
                before.part_of_speech.startswith("形容詞")
                or (before.part_of_speech.startswith("助動詞") and before.base_form in ("ない", "たい", "らしい", "た")))
            if token.surface == "です":
                if after_adjective or _is(nxt, "助詞", surface="か"):
                    i += 1
                else:
                    out.append("だ")
                    i += 1
                continue
            if (token.surface == "でし" and _is(nxt, "助動詞", "特殊・タ") and not after_adjective
                    and not _is(before, "助動詞", surface="ん")):
                out.append("だった")
                i += 2
                continue
            if token.surface == "でしょ" and _is(nxt, "助動詞", surface="う"):
                out.append("だろう")
                i += 2
                continue
        for char in token.surface:
            if char in "「『":
                depth += 1
            elif char in "」』" and depth:
                depth -= 1
        out.append(token.surface)
        i += 1
    return "".join(out), depth


def _depth_after(text: str, depth: int) -> int:
    for char in text:
        if char in "「『":
            depth += 1
        elif char in "」』" and depth:
            depth -= 1
    return depth


def polish_pieces(pieces: list[str]) -> list[str]:
    """Polish consecutive sentences of one text; each output piece renders the same input piece."""
    out = [typography(piece) for piece in pieces]
    # Spaces at the joins between sentences, where each piece alone cannot see its neighbour.
    for k in range(1, len(out)):
        if out[k - 1].rstrip(" 　\t")[-1:] and re.match(rf"[{_JA}]", out[k - 1].rstrip(" 　\t")[-1]):
            if re.match(rf"[ 　\t]+[{_JA}]", out[k]):
                out[k] = out[k].lstrip(" 　\t")
            elif out[k][:1] and re.match(rf"[{_JA}]", out[k][0]):
                out[k - 1] = out[k - 1].rstrip(" 　\t")
    analyser = _analyser() if any(_POLITE.search(piece) for piece in out) else None
    if analyser is None:
        return out
    depth = 0
    for k, piece in enumerate(out):
        if not _POLITE.search(piece):
            depth = _depth_after(piece, depth)
            continue
        try:
            tokens = list(analyser.tokenize(piece))
        except Exception:  # noqa: BLE001 - leave the sentence as it came
            depth = _depth_after(piece, depth)
            continue
        if "".join(token.surface for token in tokens) != piece:
            depth = _depth_after(piece, depth)
            continue
        out[k], depth = _restyle(tokens, depth)
    return out


def polish(text: str) -> str:
    return "".join(polish_pieces([text])) if text else text
