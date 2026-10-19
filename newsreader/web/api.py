"""JSON API used by article_translate.js. Behaviour is v3's; only the article text source changed
(the index instead of re-parsing the saved HTML on every request).

CSRF and Host checks happen centrally in ``security`` before any of these run.
"""
from __future__ import annotations

import re

from flask import abort, jsonify, request

from newsreader.library import index
from newsreader.services.dictionary import british_ipa, lookup_chinese, lookup_korean_hanja
from newsreader.services.translation import (
    MAX_ARTICLE_CHARS, MAX_SELECTION_CHARS, TARGETS, TranslationError, dictionary_available, dictionary_lookup,
    translate_paragraphs, translate_text, translate_titles,
)
from newsreader.sources import ReaderError
from newsreader.web.pages import db_path, load_article
from newsreader.web.routing import Routes

routes = Routes()


def _json_request() -> dict:
    if not request.is_json:
        abort(400)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400)
    return payload


def _article_request():
    """(target, article) for the article-level endpoints, or a ready error response."""
    payload = _json_request()
    target = payload.get("target")
    article_id = payload.get("id")
    if not isinstance(target, str) or target not in TARGETS or not isinstance(article_id, int) or isinstance(article_id, bool):
        return None, (jsonify({"error": "記事または翻訳先の指定が不正です。"}), 400)
    record = index.get(db_path(), article_id)
    if record is None:
        return None, (jsonify({"error": "記事が見つかりません。"}), 404)
    try:
        return (target, load_article(record)), None
    except (ReaderError, OSError) as exc:
        message = str(exc) if isinstance(exc, ReaderError) else "記事ファイルを読み取れません。"
        return None, (jsonify({"error": message}), 400)


@routes.post("/api/translate", "translate_selection")
def translate_selection():
    payload = _json_request()
    text = payload.get("text")
    target = payload.get("target")
    if not isinstance(text, str) or not isinstance(target, str) or target not in TARGETS:
        return jsonify({"error": "翻訳する文章または翻訳先の指定が不正です。"}), 400
    response = {"translation": None, "translation_error": None, "ipa": None, "hanja": [], "zhuyin": None, "dictionary_url": None}
    try:
        response["translation"] = translate_text(text, target, limit=MAX_SELECTION_CHARS)
    except TranslationError as exc:
        response["translation_error"] = str(exc)
    if payload.get("plain") is True:  # a whole sentence for context: no dictionary lookups
        return jsonify(response)

    hangul = len(re.findall(r"[가-힣]", text))
    han = len(re.findall(r"[㐀-䶿一-鿿豈-﫿]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if hangul:
        dictionary = lookup_korean_hanja(text)
        response["hanja"] = dictionary["entries"]
        response["dictionary_url"] = dictionary["source_url"]
        response["dictionary_label"] = dictionary["source_label"]
        response["dictionary_configured"] = dictionary["configured"]
    elif han and han >= latin:
        dictionary = lookup_chinese(text)
        response["zhuyin"] = dictionary["zhuyin"]
        response["dictionary_entries"] = dictionary.get("entries", [])
        response["dictionary_url"] = dictionary["source_url"]
        response["dictionary_label"] = dictionary["source_label"]
        if dictionary.get("entries"):
            response["meaning"] = "\n".join(entry["definition"] for entry in dictionary["entries"] if entry.get("definition")) or None
            response["meaning_language"] = "教育部辞典・繁體中文"
        if not response.get("meaning") and target == "zh-hant":
            try:
                response["meaning"] = translate_text(text, "ja", limit=MAX_SELECTION_CHARS)
                response["meaning_language"] = "日本語"
            except TranslationError:
                response["meaning"] = None
    elif latin:
        response["ipa"] = british_ipa(text)
        response["ipa_label"] = "英国英語 · eSpeak NG IPA"
        # The senses come from a second request (/api/dictionary) so the translation shows first.
        response["dictionary"] = dictionary_available(text, target)
    return jsonify(response)


@routes.post("/api/dictionary", "dictionary_senses")
def dictionary_senses():
    """Senses of one English word or short idiom by part of speech (Azure Translator dictionary)."""
    payload = _json_request()
    text, target = payload.get("text"), payload.get("target")
    if not isinstance(text, str) or not isinstance(target, str) or target not in TARGETS:
        return jsonify({"error": "語または翻訳先の指定が不正です。"}), 400
    if not dictionary_available(text, target):
        return jsonify({"senses": [], "available": False})
    try:
        senses = dictionary_lookup(text, target)
    except TranslationError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"senses": senses, "available": True, "label": "Azure Translator 辞書"})


@routes.post("/api/translate-article", "translate_whole_article")
def translate_whole_article():
    found, error = _article_request()
    if error:
        return error
    target, article = found
    try:
        translated = translate_text("\n\n".join(article["paragraphs"]), target, limit=MAX_ARTICLE_CHARS)
    except TranslationError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"translation": translated, "title": article["title"]})


@routes.post("/api/translate-paragraphs", "translate_article_paragraphs")
def translate_article_paragraphs():
    """Per-paragraph translation for the side-by-side (対訳) view; the title is item 0."""
    found, error = _article_request()
    if error:
        return error
    target, article = found
    try:
        translated = translate_paragraphs([article["title"], *article["paragraphs"]], target, limit=MAX_ARTICLE_CHARS)
    except TranslationError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"title": translated[0], "paragraphs": translated[1:]})


#: One list page (24 cards) plus room; more ids in one request are refused.
MAX_TITLES_PER_REQUEST = 40


@routes.post("/api/translate-titles", "translate_list_titles")
def translate_list_titles():
    """Headline translations for the cards on a list page: {"translations": {id: text}}."""
    payload = _json_request()
    ids, target = payload.get("ids"), payload.get("target")
    if (not isinstance(target, str) or target not in TARGETS or not isinstance(ids, list)
            or not 0 < len(ids) <= MAX_TITLES_PER_REQUEST
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
        return jsonify({"error": "記事または翻訳先の指定が不正です。"}), 400
    records = [(i, index.get(db_path(), i)) for i in dict.fromkeys(ids)]
    found = [(i, record["title"]) for i, record in records if record and record.get("title")]
    if not found:
        return jsonify({"translations": {}})
    try:
        translated = translate_titles([title for _, title in found], target)
    except TranslationError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"translations": {str(i): text for (i, _), text in zip(found, translated) if text}})


#: A same-news page compares at most this many openings at once, each cut to LEAD_CHARS.
MAX_LEADS_PER_REQUEST = 12
LEAD_CHARS = 600


def opening(article: dict) -> str:
    """The first paragraph, or the first two when the first is only a short dateline/standfirst."""
    paragraphs = [p for p in article.get("paragraphs", []) if p.strip()]
    text = paragraphs[0] if paragraphs else ""
    if len(text) < 80 and len(paragraphs) > 1:
        text = f"{text} {paragraphs[1]}"
    return text[:LEAD_CHARS]


@routes.post("/api/compare-translate", "compare_translate")
def compare_translate():
    """読み比べ: headlines and openings of articles in other languages, translated into the
    language of one outlet of the group: {"items": {id: {"title", "lead"}}}. Articles already in
    that language are left out. Cached per headline / opening like the other comparisons."""
    payload = _json_request()
    ids, target = payload.get("ids"), payload.get("target")
    if (not isinstance(target, str) or target not in TARGETS or not isinstance(ids, list)
            or not 0 < len(ids) <= MAX_LEADS_PER_REQUEST
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
        return jsonify({"error": "記事または翻訳先の指定が不正です。"}), 400
    from newsreader.library import stories
    wanted = []
    for article_id in dict.fromkeys(ids):
        record = index.get(db_path(), article_id)
        if record is None:
            continue
        if stories.LANG_TARGETS.get(stories.headline_language(db_path(), article_id), "") == target:
            continue
        content = index.get_content(db_path(), article_id)
        wanted.append((article_id, record["title"], opening(content) if content else ""))
    if not wanted:
        return jsonify({"items": {}})
    try:
        titles = translate_titles([title for _, title, _ in wanted], target)
        leads = translate_titles([lead for _, _, lead in wanted], target, kind="lead", max_chars=LEAD_CHARS)
    except TranslationError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"items": {str(i): {"title": t, "lead": l} for (i, _, _), t, l in zip(wanted, titles, leads)}})


@routes.post("/api/translate-leads", "translate_story_leads")
def translate_story_leads():
    """Opening paragraphs of the articles on a same-news page: {"leads": {id: {"original", "translation"}}}."""
    payload = _json_request()
    ids, target = payload.get("ids"), payload.get("target")
    if (not isinstance(target, str) or target not in TARGETS or not isinstance(ids, list)
            or not 0 < len(ids) <= MAX_LEADS_PER_REQUEST
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
        return jsonify({"error": "記事または翻訳先の指定が不正です。"}), 400
    found = []
    for article_id in dict.fromkeys(ids):
        content = index.get_content(db_path(), article_id)
        text = opening(content) if content else ""
        if text:
            found.append((article_id, text))
    if not found:
        return jsonify({"leads": {}})
    try:
        translated = translate_titles([text for _, text in found], target, kind="lead", max_chars=LEAD_CHARS)
    except TranslationError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"leads": {str(i): {"original": text, "translation": tr} for (i, text), tr in zip(found, translated)}})
