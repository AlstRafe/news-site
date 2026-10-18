"""/stories (groups), /stories/<key> (one group, a column per outlet) and the refresh API (§10)."""
from __future__ import annotations

import threading

from flask import abort, current_app, jsonify, render_template, request

from newsreader.library import stories, status
from newsreader.services import images as image_service
from newsreader.web.assets import CACHE_PAGE
from newsreader.web.pages import db_path, image_url
from newsreader.web.routing import Routes

routes = Routes()
_refresh_thread: threading.Thread | None = None
_refresh_guard = threading.Lock()


def _with_thumbs(members: list[dict]) -> list[dict]:
    for member in members:
        thumb = member.get("thumb_src", "")
        member["thumb"] = image_url(member["id"], 0, thumb) if image_service.allowed_image_url(thumb) else ""
    return members


@routes.get("/stories", "stories_page")
def stories_page():
    scope = "all" if request.args.get("scope") == "all" else "multi"
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    result = stories.list_stories(db_path(), scope=scope, page=page)
    for story in result["stories"]:
        _with_thumbs(story["members"])
    info = status.read(current_app.config["STATUS_PATH"]).get("stories") or {}
    response = current_app.make_response(render_template("stories.html", result=result, info=info,
                                                         refreshed_ago=status.ago(info.get("refreshed", ""))))
    response.headers["Cache-Control"] = "no-cache"
    return response


@routes.get("/stories/<key>", "story_page")
def story_page(key: str):
    story = stories.get_story(db_path(), key[:32])
    if story is None:
        abort(404)
    _with_thumbs(story["members"])
    response = current_app.make_response(render_template("story.html", story=story))
    response.headers["Cache-Control"] = CACHE_PAGE
    return response


@routes.post("/api/stories/refresh", "stories_refresh")
def stories_refresh():
    """Start a regrouping in the background; 202 at once (it may translate headlines)."""
    global _refresh_thread
    from newsreader.library import pipeline
    with _refresh_guard:
        if _refresh_thread is not None and _refresh_thread.is_alive():
            return jsonify({"started": False, "running": True}), 202
        db, status_path = db_path(), current_app.config["STATUS_PATH"]
        _refresh_thread = threading.Thread(target=pipeline.refresh_stories, args=(db, status_path),
                                           name="stories-refresh", daemon=True)
        _refresh_thread.start()
    return jsonify({"started": True, "running": True}), 202


@routes.get("/api/stories/status", "stories_status")
def stories_status():
    info = status.read(current_app.config["STATUS_PATH"]).get("stories") or {}
    running = _refresh_thread is not None and _refresh_thread.is_alive()
    return jsonify({**info, "running": running})
