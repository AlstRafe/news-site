"""What happens after articles change: index, then same-news groups.

Used by the collector (right after each collection) and by the web app's background sync
(when files changed). A failure of the grouping step never fails the indexing step, and
neither ever fails a collection (§12).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path

from newsreader.library import compare, facts_watch, index, status, stories, stories_core

log = logging.getLogger("newsreader.pipeline")


def _code_of(*modules) -> str:
    digest = hashlib.sha256()
    for module in modules:
        digest.update(Path(module.__file__).read_bytes())
    return digest.hexdigest()[:16]


#: The code that makes the groups and their figures, read when this process loaded it: a deploy that
#: changes it makes the web server regroup at its next start (``after_sync``).
GROUPING_CODE = _code_of(stories, stories_core, facts_watch, compare)


def refresh_stories(db_path: Path, status_path: Path | None = None) -> dict | None:
    """Re-group with the current settings; headline translation only if a key is configured."""
    from newsreader.services.settings import SecretStoreError, get_story_settings
    from newsreader.services.translation import provider_ready, translate_titles_to_english

    settings = get_story_settings()
    try:
        translate = translate_titles_to_english if provider_ready() else None
    except SecretStoreError:
        translate = None
    try:
        result = stories.refresh(
            db_path,
            translate=translate,
            enabled=settings["stories_enabled"],
            daily_chars=settings["stories_daily_chars"],
            status_path=status_path,
        )
    except Exception:
        log.exception("Same-news grouping failed")
        return None
    if settings["stories_enabled"]:
        refresh_facts(db_path, status_path)
    status.update("stories", {"code": GROUPING_CODE}, path=status_path or Path(db_path).parent / "status.json")
    return result


def groups_current(status_path: Path) -> bool:
    """Whether the stored groups were made by this code after the last indexing pass (status.json),
    so that regrouping now would make them again as they are."""
    data = status.read(status_path)
    made = data.get("stories") or {}
    synced = (data.get("library") or {}).get("synced", "")
    return bool(made.get("refreshed")) and made.get("code") == GROUPING_CODE and made["refreshed"] >= synced


def after_sync(result: dict, db_path: Path, status_path: Path) -> dict | None:
    """The web server's step after an index pass that changed articles, and after its first pass.

    The first pass after a start finds nothing new as a rule (the collector indexes and regroups what
    it saves), and regrouping then cost a small server ~40 s of its whole CPU and 300 MB of memory
    on every deploy. So it is left out while the stored groups are current (``groups_current``)."""
    if not (result.get("added") or result.get("updated") or result.get("removed")) and groups_current(status_path):
        log.info("Same-news groups are current: not regrouped")
        return None
    return refresh_stories(db_path, status_path)


def keep_warm(db_path: Path, interval: float = 60.0) -> threading.Thread:
    """In the web server: prepare what the pages ask for whenever the groups change (the
    collector regroups in its own process, so this looks rather than waits to be told). Each
    round is cheap when nothing changed (stories.arc keeps its results per state)."""

    def loop() -> None:
        warmed: tuple | None = None
        while True:
            now = index.db_stamp(db_path)  # unchanged index: nothing to prepare (no query at all)
            if now != warmed:
                try:
                    stories.warm_arcs(db_path)
                    warmed = now
                except Exception:
                    log.exception("Preparing 続報 failed")
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="warm-pages", daemon=True)
    thread.start()
    return thread


def refresh_facts(db_path: Path, status_path: Path | None = None) -> dict | None:
    """Compare the counts of people in new multi-outlet groups (D-59, D-65: no translation);
    never fails the caller."""
    try:
        result = facts_watch.refresh(db_path)
    except Exception:
        log.exception("Comparing figures failed")
        return None
    status.update(
        "facts", {**result, "refreshed": status.now_iso()}, path=status_path or Path(db_path).parent / "status.json"
    )
    return result


def refresh(library_root: Path, db_path: Path, status_path: Path | None = None) -> dict:
    result = {"index": None, "stories": None}
    try:
        result["index"] = index.sync(library_root, db_path, force=True)
    except Exception:
        log.exception("Index sync failed")
        return result
    result["stories"] = refresh_stories(db_path, status_path)
    return result
