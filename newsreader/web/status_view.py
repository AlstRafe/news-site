"""/status and the header badge: is collection running, what does the library hold (§7).

Everything here reads status.json (one stat when unchanged), the index's aggregate
queries and in-process sync state — never the article files.
"""
from __future__ import annotations

import platform
import re
import time
from datetime import datetime

from flask import current_app, render_template

from newsreader import paths
from newsreader.library import index, status
from newsreader.services.cache import monthly_translation_usage
from newsreader.web.assets import CACHE_LIST
from newsreader.web.routing import Routes

routes = Routes()
_STARTED = time.time()


def local_version(text: str) -> str:
    """"phase-9 (abc1234) 2026-10-18 18:43 +0000" with the time shown in this PC's timezone
    (the deployment tool may run in another timezone)."""
    match = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2} [+-]\d{4})$", text or "")
    if not match:
        return text
    try:
        moment = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M %z").astimezone()
    except ValueError:
        return text
    return text[: match.start()] + moment.strftime("%Y-%m-%d %H:%M")


def status_data() -> dict:
    return status.read(current_app.config["STATUS_PATH"])


def init_app(app) -> None:
    @app.context_processor
    def _badge():
        return {"status_badge": status.badge(status_data())}


@routes.get("/status", "status_page")
def status_page():
    data = status_data()
    collector = data.get("collector") or {}
    db = current_app.config["LIBRARY_DB"]
    sites = [{"name": name, **values} for name, values in sorted((collector.get("sites") or {}).items())]
    response = current_app.make_response(render_template(
        "status.html",
        collector=collector, collector_ago=status.ago(collector.get("finished", "")), sites=sites,
        library=data.get("library") or {}, sources=index.source_stats(db), index_stats=index.index_stats(db),
        stories_info=data.get("stories") or {}, stories_ago=status.ago((data.get("stories") or {}).get("refreshed", "")),
        sync=index.sync_state(db), usage=monthly_translation_usage(),
        version=local_version(paths.deployed_version()) or "（開発版）", python=platform.python_version(),
        started=time.strftime("%Y-%m-%d %H:%M", time.localtime(_STARTED)),
    ))
    response.headers["Cache-Control"] = CACHE_LIST
    return response
