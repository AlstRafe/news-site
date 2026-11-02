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

INSTANCE_DIRECTORY = paths.INSTANCE_DIR
COLLECTOR_SETTINGS = paths.COLLECTOR_SETTINGS_PATH
LOCK_FILE = paths.COLLECTOR_LOCK_PATH


def get_library_root() -> Path:
    try:
        settings = json.loads(COLLECTOR_SETTINGS.read_text(encoding="utf-8"))
        configured = settings.get("library_root", "") if isinstance(settings, dict) else ""
    except (OSError, ValueError, TypeError):
        configured = ""
    # The collector takes its library root from collector.json (written by launcher.py), not
    # from HTML_DIRECTORY (which only the web app reads); then the settings-page folder.
    configured = configured or paths.configured_library_dir()
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


#: Another installation that collected into the same folder this recently does the job.
OTHER_COLLECTOR_MINUTES = 90


def collected_elsewhere(library_root: Path) -> bool:
    """True when the installation that owns ``library_root`` (its instance/status.json beside the
    folder) collected into it successfully within the last OTHER_COLLECTOR_MINUTES: this copy's
    hourly task then does nothing instead of fetching the same pages again."""
    other = paths.status_path_beside(library_root)
    try:
        if other.resolve() == status_path().resolve() or not other.is_file():
            return False
    except OSError:
        return False
    collector = status.read(other).get("collector") or {}
    recorded = collector.get("library_root")
    if not collector.get("ok") or (recorded and not paths.same_path(recorded, library_root)):
        return False
    minutes = status.minutes_since(collector.get("finished", ""))
    return minutes is not None and minutes < OTHER_COLLECTOR_MINUTES


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
        if collected_elsewhere(library_root):
            log.info("Skipped: the installation that owns %s collected into it recently", library_root)
            status.update(
                "collector",
                {
                    "running": False,
                    "finished": status.now_iso(),
                    "ok": True,
                    "error": "",
                    "seconds": round(time.perf_counter() - started, 1),
                    "sites": {},
                    "skipped_for": str(paths.status_path_beside(library_root).parent.parent),
                    "library_root": str(library_root),
                },
                path=status_path(),
            )
            update_library_index(library_root)
            from newsreader.library import pipeline

            pipeline.refresh_stories(library.DEFAULT_DB_PATH, status_path())
            return 0
        log.info("Standalone scheduled collection started: %s", library_root)
        report: dict = {}
        from newsreader.services.settings import library_limit  # late: keyring only when needed

        totals = fetch_cycle(
            library_root,
            report=report,
            skip_memory_path=INSTANCE_DIRECTORY / "collector_skipped.json",
            limit=library_limit(),
            db_path=library.DEFAULT_DB_PATH,
        )
        log.info("Standalone scheduled collection complete: %s", totals)
        status.update(
            "collector",
            {
                "running": False,
                "finished": status.now_iso(),
                "ok": True,
                "error": "",
                "seconds": round(time.perf_counter() - started, 1),
                "sites": report,
                "library_root": str(library_root),
            },
            path=status_path(),
        )
        update_library_index(library_root)
        from newsreader.library import pipeline  # imported late: translation/keyring only when needed

        pipeline.refresh_stories(library.DEFAULT_DB_PATH, status_path())
        print("Article collection complete:", ", ".join(f"{source}={count}" for source, count in totals.items()))
        return 0
    except Exception as exc:
        log.exception("Standalone scheduled collection failed")
        status.update(
            "collector",
            {
                "running": False,
                "finished": status.now_iso(),
                "ok": False,
                "error": f"自動取得が失敗しました（{type(exc).__name__}）",
                "seconds": round(time.perf_counter() - started, 1),
            },
            path=status_path(),
        )
        return 1
    finally:
        lock_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
