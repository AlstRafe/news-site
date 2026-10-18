"""One-shot article collector, intended to run as a Windows scheduled task.

The root ``collector.py`` is a thin wrapper over ``main()`` here: the Windows task
``NewsHtmlReader-HourlyCollector-ef29a11a`` runs ``pythonw.exe collector.py --once``
and that command line must keep working.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from newsreader import paths
from newsreader.collect.autofetch import configure_logging, fetch_cycle
from newsreader.library import index as library
from newsreader.services.settings import get_secret, get_settings

INSTANCE_DIRECTORY = paths.INSTANCE_DIR
COLLECTOR_SETTINGS = paths.COLLECTOR_SETTINGS_PATH
LOCK_FILE = paths.COLLECTOR_LOCK_PATH


def get_library_root() -> Path:
    try:
        settings = json.loads(COLLECTOR_SETTINGS.read_text(encoding="utf-8"))
        configured = settings.get("library_root", "") if isinstance(settings, dict) else ""
    except (OSError, ValueError, TypeError):
        configured = ""
    # Unchanged on purpose: the collector takes its library root from collector.json,
    # not from HTML_DIRECTORY (which only the web app reads).
    return Path(configured).expanduser().resolve() if configured else paths.DEFAULT_ARTICLES_DIR.resolve()


def acquire_process_lock():
    """Return an open locked file, or None when another collector is active."""
    INSTANCE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    lock_file = LOCK_FILE.open("a+b")
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    lock_file.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        lock_file.close()
        return None
    return lock_file


def update_library_index(library_root: Path) -> dict | None:
    """Index newly saved articles at once so the web app shows them without waiting for its own scan."""
    log = logging.getLogger("news_autofetch")
    try:
        result = library.sync(library_root, library.DEFAULT_DB_PATH, force=True)
    except Exception:  # an index problem must not turn a successful collection into a failure
        log.exception("Library index update failed")
        return None
    log.info("Library index updated: %s", result)
    return result


def main() -> int:
    if sys.argv[1:] not in ([], ["--once"]):
        print("Usage: collector.py [--once]", file=sys.stderr)
        return 2
    lock_file = acquire_process_lock()
    if lock_file is None:
        print("An article collection cycle is already running; skipping this trigger.")
        return 0
    try:
        configure_logging(INSTANCE_DIRECTORY)
        library_root = get_library_root()
        naver_client_id = get_secret("naver_api_key_id")
        naver_client_secret = get_secret("naver_api_key_secret")
        naver_credentials = (naver_client_id, naver_client_secret) if naver_client_id and naver_client_secret else None
        naver_query = get_settings().get("naver_search_query", "뉴스")
        logging.getLogger("news_autofetch").info("Standalone scheduled collection started: %s", library_root)
        totals = fetch_cycle(library_root, naver_credentials, naver_query)
        logging.getLogger("news_autofetch").info("Standalone scheduled collection complete: %s", totals)
        update_library_index(library_root)
        print("Article collection complete:", ", ".join(f"{source}={count}" for source, count in totals.items()))
        return 0
    except Exception:
        logging.getLogger("news_autofetch").exception("Standalone scheduled collection failed")
        return 1
    finally:
        lock_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
