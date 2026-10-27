"""保存（後で読む）, 既読 and 単語帳: the reader's own marks, kept across the PC and the phone."""
from __future__ import annotations

from flask import abort, current_app, jsonify, render_template, request

from newsreader.library import index, reading
from newsreader.services import images as image_service
from newsreader.services.translation import TARGETS
from newsreader.web.pages import db_path, image_url
from newsreader.web.routing import Routes

routes = Routes()


def reader_db():
    return reading.path_for(current_app.config["LIBRARY_DB"])


def _payload() -> dict:
    payload = request.get_json(silent=True) if request.is_json else None
    if not isinstance(payload, dict):
        abort(400)
    return payload


def _article(payload: dict) -> dict:
    article_id = payload.get("id")
    if not isinstance(article_id, int) or isinstance(article_id, bool):
        abort(400)
    record = index.get(db_path(), article_id)
    if record is None:
        abort(404)
    return record


@routes.post("/api/saved", "save_article")
def save_article():
    """{"id": n, "saved": true|false} -> {"saved": bool}"""
    payload = _payload()
    record = _article(payload)
    on = payload.get("saved") is True
    reading.set_saved(reader_db(), record, on)
    return jsonify({"saved": on})


@routes.get("/saved", "saved_page")
def saved_page():
    items = []
    for mark in reading.saved(reader_db()):
        record = index.find_by_file(db_path(), mark["source"], mark["filename"])
        if record is None:  # the file is gone: still listed, without a link
            items.append({**mark, "id": 0})
            continue
        thumb = record.get("thumb_src", "")
        items.append({**record, "saved_at": mark["saved_at"], "read": mark["read"],
                      "thumb": image_url(record["id"], 0, thumb) if image_service.allowed_image_url(thumb) else ""})
    from newsreader.services import translation
    from newsreader.web.pages import page_target
    for item, ja in zip(items, translation.cached_titles([i.get("title") or "" for i in items], page_target())):
        item["ja"] = ja
    response = current_app.make_response(render_template("saved.html", items=items))
    response.headers["Cache-Control"] = "no-cache"
    return response


@routes.post("/api/words", "add_word")
def add_word():
    """{"word", "target", "meaning", "sentence", "id"?}: kept in the 単語帳 (again: updated)."""
    payload = _payload()
    word, target = payload.get("word"), payload.get("target")
    meaning, sentence = payload.get("meaning", ""), payload.get("sentence", "")
    if (not isinstance(word, str) or not word.strip() or len(word) > 80 or target not in TARGETS
            or not isinstance(meaning, str) or not isinstance(sentence, str)):
        return jsonify({"error": "単語の指定が不正です。"}), 400
    record = None
    if payload.get("id") is not None:
        record = _article(payload)
    reading.add_word(reader_db(), word, target, meaning, sentence, record)
    return jsonify({"saved": True})


@routes.post("/api/words/delete", "remove_word")
def remove_word():
    payload = _payload()
    word_id = payload.get("word_id")
    if not isinstance(word_id, int) or isinstance(word_id, bool):
        abort(400)
    reading.remove_word(reader_db(), word_id)
    return jsonify({"removed": True})


@routes.get("/words", "words_page")
def words_page():
    entries = reading.words(reader_db())
    for entry in entries:
        source, _, filename = entry["article_key"].partition("/")
        record = index.find_by_file(db_path(), source, filename) if filename else None
        entry["article_id"] = record["id"] if record else 0
    response = current_app.make_response(render_template("words.html", entries=entries))
    response.headers["Cache-Control"] = "no-cache"
    return response
