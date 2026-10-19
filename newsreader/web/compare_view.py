"""読み比べ「違いを見る」(D-56): numbers and words compared across the outlets of one news group.

Both answer on request (a button on the group page), translate only what they show, and reuse
every cache: an English outlet's own text is never sent; other outlets' openings go to English
once (kind "facts_src"); the sentences and words shown go to the reader's language once each.
"""
from __future__ import annotations

import re

from flask import jsonify

from newsreader import branding
from newsreader.library import compare, index, stories
from newsreader.services import sentences
from newsreader.services.translation import TARGETS, TranslationError, translate_titles
from newsreader.web.api import _json_request
from newsreader.web.pages import db_path
from newsreader.web.routing import Routes

routes = Routes()

#: The newest articles of a group that are compared (the group page lists them all).
MAX_MEMBERS = 12
#: Opening read for figures, per article: the first paragraphs carry the main figures; later
#: ones often report side events (another attack, last year's toll).
FACT_PARAGRAPHS = 3
FACT_CHARS = 1_000
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


def _opening(content: dict | None) -> str:
    text = ""
    taken = [p.strip() for p in (content or {}).get("paragraphs", []) if p.strip()][:FACT_PARAGRAPHS]
    for paragraph in taken:
        if text and len(text) + len(paragraph) > FACT_CHARS:
            break
        text = f"{text}\n{paragraph}" if text else paragraph
    return text[:FACT_CHARS]


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
    members = sorted(story["members"], key=lambda m: m["published"] or "", reverse=True)[:MAX_MEMBERS]
    english_of: dict[int, str] = {}
    originals: dict[int, list[tuple[str, str]]] = {}  # id -> [(own sentence, English sentence)]
    foreign = []
    for member in members:
        opening = _opening(index.get_content(db_path(), member["id"]))
        if member.get("lang") == "en":
            english_of[member["id"]] = f"{opening}\n{member['title']}"  # body first: fuller sentences win
        elif member.get("pivot"):
            foreign.append((member, opening))
    try:
        translated = translate_titles([opening for _, opening in foreign], "en", kind="facts_src", max_chars=FACT_CHARS)
    except TranslationError as exc:
        return jsonify({"error": str(exc)}), 503
    for (member, opening), english in zip(foreign, translated):
        english_of[member["id"]] = f"{english}\n{member['pivot']}"
        pairs = [(member["title"], member["pivot"])]
        pairs += sentences.pairs("en", opening.strip()[:FACT_CHARS], english) or []
        originals[member["id"]] = pairs
    by_id = {member["id"]: member for member in members}
    categories = compare.compare([dict(member, english=english_of[member["id"]])
                                  for member in members if member["id"] in english_of])
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
