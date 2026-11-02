"""読み比べ「違いを見る」(D-56): numbers and words compared across the outlets of one news group.

Both answer on request (a button on the group page), translate only what they show, and reuse
every cache: an English outlet's own text is never sent; other outlets' openings go to English
once (kind "facts_src"); the sentences and words shown go to the reader's language once each.
"""

from __future__ import annotations

import re

from flask import jsonify

from newsreader import branding, i18n
from newsreader.library import compare, facts_watch, stories
from newsreader.services import sentences
from newsreader.services.translation import TARGETS, TranslationError, translate_titles
from newsreader.web.api import _json_request
from newsreader.web.pages import db_path
from newsreader.web.routing import Routes

routes = Routes()

FACTS_PER_ROW = 3
_KEY = re.compile(r"s\d{1,9}")


def _story(payload: dict):
    key, target = payload.get("key"), payload.get("target")
    if not isinstance(key, str) or not _KEY.fullmatch(key) or not isinstance(target, str) or target not in TARGETS:
        return None, (jsonify({"error": "ニュースまたは翻訳先の指定が不正です。"}), 400)
    story = stories.get_story(db_path(), key)
    if story is None:
        return None, (jsonify({"error": "このニュースのグループは見つかりませんでした。"}), 404)
    return (story, target), None


def _to_english(texts: list[str]) -> list[str]:
    return translate_titles(texts, "en", kind="facts_src", max_chars=facts_watch.FACT_CHARS)


def _same(a: str, b: str) -> bool:
    a, b = " ".join(a.split()), " ".join(b.split())
    return bool(a and b) and (a in b or b in a)


def _english_texts(story: dict):
    """(members, {id: English text}, {id: [(own sentence, English sentence)]}) for a group."""
    members = facts_watch.newest_members(story)
    english_of, foreign = facts_watch.english_of(db_path(), members, _to_english)
    originals: dict[int, list[tuple[str, str]]] = {}
    for member in members:
        if member["id"] in foreign:
            own, english = foreign[member["id"]]
            originals[member["id"]] = [(member["title"], member["pivot"])] + (sentences.pairs("en", own, english) or [])
    return members, english_of, originals


def _show_sentences(items: list[tuple[dict, dict]], originals: dict, target: str) -> None:
    """For each (member, item with an English "sentence"): set "original" (the outlet's own
    sentence, when known) and "shown" (in the reader's language: translated once from the
    outlet's own sentence, else from the English one). Raises TranslationError."""
    wanted: dict[str, str] = {}
    for member, item in items:
        own = (
            item["sentence"]
            if item.get("native")
            else next((o for o, e in originals.get(member["id"], []) if _same(e, item["sentence"])), "")
        )
        item["original"] = own
        if stories.LANG_TARGETS.get(member.get("lang") or "", "") == target:
            item["shown"] = own or item["sentence"]
        elif target == "en" and not own:
            item["shown"] = item["sentence"]
        else:
            wanted.setdefault(own or item["sentence"], "")
            item["shown_from"] = own or item["sentence"]
    if wanted:
        texts = list(wanted)
        for text, out in zip(texts, translate_titles(texts, target, kind="fact", max_chars=600)):
            wanted[text] = out
    for _, item in items:
        if "shown_from" in item:
            item["shown"] = wanted.get(item.pop("shown_from"), "")


def _outlet_fields(row: dict) -> None:
    row["lang"] = branding.source_language(row["source"])  # badge colour
    row["tag"] = branding.LANG_TAGS.get(row["lang"], "")  # the original sentence's lang


@routes.post("/api/story-facts", "story_facts")
def story_facts():
    """{"categories": [...]} — see compare.compare; each fact also carries the sentence in the
    reader's language ("shown") and, for a non-English outlet, its own sentence ("original")."""
    found, error = _story(_json_request())
    if error:
        return error
    story, target = found
    try:
        members, english_of, originals = _english_texts(story)
        by_id = {member["id"]: member for member in members}
        native = facts_watch.native_texts(db_path(), members)  # counts read in Korean/Chinese (D-65)
        categories = compare.compare(
            [
                dict(member, english=english_of[member["id"]], native=native.get(member["id"]))
                for member in members
                if member["id"] in english_of
            ]
        )
        facts_watch.store(db_path(), story["key"], members, categories)  # the home page's 数字の食い違い
        items = []
        for category in categories:
            for row in category["rows"]:
                row["facts"] = row["facts"][:FACTS_PER_ROW]
                _outlet_fields(row)
                items += [(by_id[row["id"]], fact) for fact in row["facts"]]
        _show_sentences(items, originals, target)
    except TranslationError as exc:
        return jsonify({"error": str(exc)}), 503
    # Kept in Japanese (facts_watch above); said in the page's language
    lang = i18n.current()
    for category in categories:
        category["label"] = i18n.text(category["label"])
        for row in category["rows"]:
            for fact in row["facts"]:
                fact["text"] = compare.say_fact(fact["text"], lang)
    return jsonify({"categories": categories, "compared": len(english_of)})


@routes.post("/api/story-quotes", "story_quotes")
def story_quotes():
    """発言を比べる (D-64): {"groups": [{"outlets", "quotes": [{"id", "source", "published",
    "text", "sentence", "original", "shown", "lang", "tag"}]}]} — the same statement as each
    outlet reported (and translated) it, side by side."""
    found, error = _story(_json_request())
    if error:
        return error
    story, target = found
    try:
        members, english_of, originals = _english_texts(story)
        by_id = {member["id"]: member for member in members}
        groups = compare.quote_groups(
            [dict(member, english=english_of[member["id"]]) for member in members if member["id"] in english_of]
        )
        items = []
        for group in groups:
            for quote in group["quotes"]:
                _outlet_fields(quote)
                items.append((by_id[quote["id"]], quote))
        _show_sentences(items, originals, target)
    except TranslationError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"groups": groups, "compared": len(english_of)})


@routes.post("/api/story-words", "story_words")
def story_words():
    """{"shared": [{"word", "gloss"}], "only": [{"source", "words": [{"word", "gloss"}]}]} from the
    English headlines of the group, each word glossed in the reader's language."""
    found, error = _story(_json_request())
    if error:
        return error
    story, target = found
    framing = compare.framing(story["members"])
    words = list(dict.fromkeys(framing["shared"] + [w for ws in framing["only"].values() for w in ws]))
    glosses: dict[str, str] = {}
    if words and target != "en":
        try:
            glosses = dict(zip(words, translate_titles(words, target, kind="word", max_chars=60)))
        except TranslationError as exc:
            return jsonify({"error": str(exc)}), 503
    entry = lambda word: {
        "word": word,
        "gloss": glosses.get(word, "") if glosses.get(word, "").lower() != word.lower() else "",
    }
    return jsonify(
        {
            "shared": [entry(w) for w in framing["shared"]],
            "only": [
                {"source": source, "lang": branding.source_language(source), "words": [entry(w) for w in found_words]}
                for source, found_words in framing["only"].items()
            ],
        }
    )
