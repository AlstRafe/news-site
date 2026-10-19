"""読み比べ「違いを見る」(D-56): numbers and words compared across the outlets of one news group.

Both answer on request (a button on the group page), translate only what they show, and reuse
every cache: an English outlet's own text is never sent; other outlets' openings go to English
once (kind "facts_src"); the sentences and words shown go to the reader's language once each.
"""
from __future__ import annotations

import re

from flask import jsonify

from newsreader import branding
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


@routes.post("/api/story-facts", "story_facts")
def story_facts():
    """{"categories": [...]} — see compare.compare; each fact also carries the sentence in the
    reader's language ("shown") and, for a non-English outlet, its own sentence ("original")."""
    found, error = _story(_json_request())
    if error:
        return error
    story, target = found
    members = facts_watch.newest_members(story)
    try:
        english_of, foreign = facts_watch.english_of(db_path(), members, _to_english)
    except TranslationError as exc:
        return jsonify({"error": str(exc)}), 503
    originals: dict[int, list[tuple[str, str]]] = {}  # id -> [(own sentence, English sentence)]
    for member in members:
        if member["id"] in foreign:
            own, english = foreign[member["id"]]
            originals[member["id"]] = [(member["title"], member["pivot"])] + (sentences.pairs("en", own, english) or [])
    by_id = {member["id"]: member for member in members}
    categories = compare.compare([dict(member, english=english_of[member["id"]])
                                  for member in members if member["id"] in english_of])
    facts_watch.store(db_path(), story["key"], members, categories)  # the home page's 数字の食い違い
    # The sentences to show, each translated once into the reader's language: an outlet's own
    # sentence when known (one translation step), else the English one.
    wanted: dict[str, str] = {}
    for category in categories:
        for row in category["rows"]:
            member = by_id[row["id"]]
            row["facts"] = row["facts"][:FACTS_PER_ROW]
            for fact in row["facts"]:
                own = next((o for o, e in originals.get(row["id"], []) if _same(e, fact["sentence"])), "")
                fact["original"] = own
                same_language = stories.LANG_TARGETS.get(member.get("lang") or "", "") == target
                if same_language:
                    fact["shown"] = own or fact["sentence"]
                elif target == "en" and not own:
                    fact["shown"] = fact["sentence"]
                else:
                    wanted.setdefault(own or fact["sentence"], "")
                    fact["shown_from"] = own or fact["sentence"]
                row["lang"] = branding.source_language(row["source"])  # badge colour
                row["tag"] = branding.LANG_TAGS.get(row["lang"], "")  # the original sentence's lang
    if wanted:
        texts = list(wanted)
        try:
            for text, out in zip(texts, translate_titles(texts, target, kind="fact", max_chars=600)):
                wanted[text] = out
        except TranslationError as exc:
            return jsonify({"error": str(exc)}), 503
    for category in categories:
        for row in category["rows"]:
            for fact in row["facts"]:
                if "shown_from" in fact:
                    fact["shown"] = wanted.get(fact.pop("shown_from"), "")
    return jsonify({"categories": categories, "compared": len(english_of)})


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
    entry = lambda word: {"word": word, "gloss": glosses.get(word, "") if glosses.get(word, "").lower() != word.lower() else ""}
    return jsonify({"shared": [entry(w) for w in framing["shared"]],
                    "only": [{"source": source, "lang": branding.source_language(source),
                              "words": [entry(w) for w in found_words]}
                             for source, found_words in framing["only"].items()]})
