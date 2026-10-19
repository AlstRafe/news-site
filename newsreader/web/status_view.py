"""/status and the header badge: is collection running, what does the library hold (§7).

Everything here reads status.json (one stat when unchanged), the index's aggregate
queries and in-process sync state — never the article files.
"""
from __future__ import annotations

import platform
import re
import time
from datetime import datetime
from pathlib import Path

from flask import current_app, jsonify, render_template

from newsreader import paths
from newsreader.library import index, status, stories
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
    data = status.read(current_app.config["STATUS_PATH"])
    other = library_collector(Path(current_app.config["LIBRARY_ROOT"]), Path(current_app.config["STATUS_PATH"]))
    if other and _newer(other, data.get("collector") or {}):
        data = {**data, "collector": other}
    return data


def library_collector(library_root: Path, own_status: Path) -> dict | None:
    """The collection record kept beside the article folder (``<folder>/../instance/status.json``)
    when this app shows another installation's articles: that installation's hourly task is what
    fills the folder, so the header badge should follow it. Read only; None when not applicable."""
    candidate = paths.status_path_beside(library_root)
    try:
        if candidate.resolve() == own_status.resolve() or not candidate.is_file():
            return None
    except OSError:
        return None
    collector = status.read(candidate).get("collector")
    if not isinstance(collector, dict) or not collector.get("finished") and not collector.get("running"):
        return None
    recorded = collector.get("library_root")
    if recorded and not paths.same_path(Path(recorded), library_root):
        return None  # that task collects into another folder
    return {**collector, "elsewhere": str(candidate.parent.parent)}


def _newer(candidate: dict, current: dict) -> bool:
    if current.get("skipped_for") and candidate.get("finished"):
        return True  # this copy's task left the job to that installation
    if current.get("running"):
        return False
    if candidate.get("running"):
        return True
    return str(candidate.get("finished", "")) > str(current.get("finished", ""))


def init_app(app) -> None:
    @app.context_processor
    def _badge():
        from newsreader.services.settings import collects_hourly
        return {"status_badge": status.badge(status_data(), collects=collects_hourly())}


@routes.get("/api/version", "app_version")
def app_version():
    response = jsonify({"version": current_app.config.get("APP_VERSION", "")})
    response.headers["Cache-Control"] = "no-store"
    return response


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
        library_root=str(current_app.config["LIBRARY_ROOT"]), stories_info=data.get("stories") or {},
        story_exclusions=stories.exclusion_count(db), stories_ago=status.ago((data.get("stories") or {}).get("refreshed", "")),
        sync=index.sync_state(db), usage=monthly_translation_usage(),
        version=local_version(paths.deployed_version()) or "（開発版）", python=platform.python_version(),
        started=time.strftime("%Y-%m-%d %H:%M", time.localtime(_STARTED)),
        logon_task=paths.LOGON_TASK_RECORD.is_file(),
    ))
    response.headers["Cache-Control"] = CACHE_LIST
    return response
