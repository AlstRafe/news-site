"""/stories (groups), /stories/<key> (one group, a column per outlet) and the refresh API (§10)."""
from __future__ import annotations

import re
import threading

from flask import abort, current_app, jsonify, render_template, request

from newsreader.library import stories, status
from newsreader.services import images as image_service
from newsreader.web.pages import db_path, image_url
from newsreader.web.routing import Routes

routes = Routes()
_refresh_thread: threading.Thread | None = None
_refresh_guard = threading.Lock()


#: Timeline chart geometry (an inline SVG: the CSP forbids inline styles, attributes are fine).
CHART_ROW = 30


def timeline_chart(story: dict) -> dict | None:
    """Who reported when, one row per outlet (the first to report on top): x in percent of the
    time between the first and the last report (at least an hour)."""
    from datetime import datetime
    dated = []
    for member in story["members"]:
        try:
            dated.append((member, datetime.strptime(member["published"], "%Y-%m-%dT%H:%M:%SZ")))
        except (TypeError, ValueError):
            continue
    if len({m["source"] for m, _ in dated}) < 2:
        return None
    first, last = min(t for _, t in dated), max(t for _, t in dated)
    span = max((last - first).total_seconds(), 3600.0)
    rows: dict[str, list] = {}
    for member, moment in sorted(dated, key=lambda pair: pair[1]):
        rows.setdefault(member["source"] or "未分類", []).append(
            {"id": member["id"], "title": member["title"], "published": member["published"],
             "x": round(3 + 94 * (moment - first).total_seconds() / span, 2)})
    return {"rows": [{"source": source, "y": index * CHART_ROW + CHART_ROW // 2, "points": points}
                     for index, (source, points) in enumerate(rows.items())],
            "height": len(rows) * CHART_ROW, "first": story["oldest"], "last": story["newest_time"],
            "hours": round(span / 3600, 1)}


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
    from newsreader.library import index
    from newsreader.web.pages import query_in_english
    query = request.args.get("q", "").strip()[:100]
    english = query_in_english(query) if re.search(r"[^\x00-\x7f]", query) else ""
    result = stories.list_stories(db_path(), scope=scope, page=page, query=query,
                                  pivot_words=index.search_words(english or query))
    result["english_query"] = english
    from newsreader.library import facts_watch
    disputes = facts_watch.disputed(db_path(), [story["key"] for story in result["stories"]])
    for story in result["stories"]:
        story["disputes"] = disputes.get(story["key"], [])
        _with_thumbs(story["members"])
        # One picture per group: the lead's, else the newest member's that has one.
        ordered = sorted(story["members"], key=lambda m: m["id"] != story["lead_id"])
        story["picture"] = next((m["thumb"] for m in ordered if m["thumb"]), "")
    info = status.read(current_app.config["STATUS_PATH"]).get("stories") or {}
    response = current_app.make_response(render_template("stories.html", result=result, info=info,
                                                         refreshed_ago=status.ago(info.get("refreshed", ""))))
    response.headers["Cache-Control"] = "no-cache"
    return response


@routes.get("/outlets", "outlets_page")
def outlets_page():
    """媒体くらべ (D-62): the outlets side by side over the last days."""
    from newsreader.library import outlets
    try:
        days = min(30, max(1, int(request.args.get("days", "7"))))
    except ValueError:
        days = 7
    response = current_app.make_response(render_template("outlets.html", stats=outlets.stats(db_path(), days=days)))
    response.headers["Cache-Control"] = "no-cache"
    return response


@routes.get("/stories/<key>", "story_page")
def story_page(key: str):
    story = stories.get_story(db_path(), key[:32])
    if story is None:
        abort(404)
    _with_thumbs(story["members"])
    from newsreader.library import facts_watch
    disputes = facts_watch.disputed(db_path(), [story["key"]]).get(story["key"], [])
    response = current_app.make_response(render_template("story.html", story=story, chart=timeline_chart(story),
                                                         disputes=disputes))
    # Not reused from the browser cache: a group changes when it is corrected or re-grouped.
    response.headers["Cache-Control"] = "no-cache"
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


@routes.post("/api/stories/exclude", "stories_exclude")
def stories_exclude():
    """"別のニュース": take one article out of a group for good, re-group, and say where to go next."""
    from newsreader.library import pipeline
    payload = request.get_json(silent=True) if request.is_json else None
    article_id = payload.get("id") if isinstance(payload, dict) else None
    key = payload.get("key") if isinstance(payload, dict) else None
    if not isinstance(article_id, int) or isinstance(article_id, bool) or not isinstance(key, str) or len(key) > 32:
        return jsonify({"error": "指定が不正です。"}), 400
    db = db_path()
    story = stories.get_story(db, key)
    remaining = [m["id"] for m in story["members"] if m["id"] != article_id] if story else []
    if not stories.exclude(db, article_id, key):
        return jsonify({"error": "この記事はこのグループに入っていません。"}), 404
    with _refresh_guard:
        pipeline.refresh_stories(db, current_app.config["STATUS_PATH"])
    # Where the rest of the group lives now (its key can change when the smallest id left).
    for other in remaining:
        group = stories.related(db, other)
        if group:
            return jsonify({"ok": True, "redirect": f"/stories/{group['key']}"})
    return jsonify({"ok": True, "redirect": "/stories"})


@routes.post("/api/stories/exclusions/clear", "stories_clear_exclusions")
def stories_clear_exclusions():
    from newsreader.library import pipeline
    cleared = stories.clear_exclusions(db_path())
    with _refresh_guard:
        pipeline.refresh_stories(db_path(), current_app.config["STATUS_PATH"])
    return jsonify({"ok": True, "cleared": cleared})
