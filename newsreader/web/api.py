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
    MAX_ARTICLE_CHARS, MAX_SELECTION_CHARS, TARGETS, TranslationError, translate_paragraphs, translate_text,
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
    return jsonify(response)


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
