"""What happens after articles change: index, then same-news groups.

Used by the collector (right after each collection) and by the web app's background sync
(when files changed). A failure of the grouping step never fails the indexing step, and
neither ever fails a collection (§12).
"""
from __future__ import annotations

import logging
from pathlib import Path

from newsreader.library import index, stories

log = logging.getLogger("newsreader.pipeline")


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
        result = stories.refresh(db_path, translate=translate, enabled=settings["stories_enabled"],
                                 daily_chars=settings["stories_daily_chars"], status_path=status_path)
    except Exception:
        log.exception("Same-news grouping failed")
        return None
    if settings["stories_enabled"]:
        refresh_facts(db_path, status_path, english=translate is not None)
    return result


def refresh_facts(db_path: Path, status_path: Path | None = None, *, english: bool = True) -> dict | None:
    """Compare the figures of new multi-outlet groups (D-59); never fails the caller."""
    from newsreader.library import facts_watch, status
    from newsreader.services.translation import translate_titles

    def to_english(texts: list[str]) -> list[str]:
        return translate_titles(texts, "en", kind="facts_src", max_chars=facts_watch.FACT_CHARS)

    try:
        result = facts_watch.refresh(db_path, to_english if english else None)
    except Exception:
        log.exception("Comparing figures failed")
        return None
    status.update("facts", {**result, "refreshed": status.now_iso()}, path=status_path or Path(db_path).parent / "status.json")
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
