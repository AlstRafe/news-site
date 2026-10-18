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
import time
from pathlib import Path

from newsreader import paths
from newsreader.collect.autofetch import configure_logging, fetch_cycle
from newsreader.library import index as library
from newsreader.library import status
from newsreader.services.settings import SecretStoreError, get_secret, get_settings

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


def status_path() -> Path:
    """status.json sits beside the index (instance/ in production, a temp dir in tests)."""
    return Path(library.DEFAULT_DB_PATH).parent / "status.json"


def update_library_index(library_root: Path) -> dict | None:
    """Index newly saved articles at once so the web app shows them without waiting for its own scan."""
    log = logging.getLogger("news_autofetch")
    try:
        result = library.sync(library_root, library.DEFAULT_DB_PATH, force=True)
        status.update("library", {"synced": status.now_iso(), "result": result}, path=status_path())
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
    log = logging.getLogger("news_autofetch")
    started = time.perf_counter()
    try:
        configure_logging(INSTANCE_DIRECTORY)
        status.update("collector", {"running": True, "started": status.now_iso()}, path=status_path())
        library_root = get_library_root()
        naver_credentials = _naver_credentials(log)
        naver_query = get_settings().get("naver_search_query", "뉴스")
        log.info("Standalone scheduled collection started: %s", library_root)
        report: dict = {}
        totals = fetch_cycle(library_root, naver_credentials, naver_query, report=report)
        log.info("Standalone scheduled collection complete: %s", totals)
        status.update("collector", {"running": False, "finished": status.now_iso(), "ok": True, "error": "",
                                    "seconds": round(time.perf_counter() - started, 1), "sites": report,
                                    "naver_configured": naver_credentials is not None}, path=status_path())
        update_library_index(library_root)
        from newsreader.library import pipeline  # imported late: translation/keyring only when needed
        pipeline.refresh_stories(library.DEFAULT_DB_PATH, status_path())
        print("Article collection complete:", ", ".join(f"{source}={count}" for source, count in totals.items()))
        return 0
    except Exception as exc:
        log.exception("Standalone scheduled collection failed")
        status.update("collector", {"running": False, "finished": status.now_iso(), "ok": False,
                                    "error": f"自動取得が失敗しました（{type(exc).__name__}）",
                                    "seconds": round(time.perf_counter() - started, 1)}, path=status_path())
        return 1
    finally:
        lock_file.close()


def _naver_credentials(log: logging.Logger) -> tuple[str, str] | None:
    """NAVER API keys from the credential store. An unavailable store skips NAVER only."""
    try:
        client_id = get_secret("naver_api_key_id")
        client_secret = get_secret("naver_api_key_secret")
    except SecretStoreError:
        log.warning("Credential store unavailable; NAVER NEWS is skipped this cycle")
        return None
    return (client_id, client_secret) if client_id and client_secret else None


if __name__ == "__main__":
    raise SystemExit(main())
